"""Booking endpoints, nested under their owning hotel.

``/hotels/{hotel_public_id}/bookings/{booking_public_id}``.

**No sub-resources for allocations or nights, and that is the schema's decision.** The
deferred night-completeness trigger requires an allocation and its nights to exist together
at COMMIT; since each request is one transaction, a route that created an allocation on its
own could never commit. Allocations and their nightly rates are therefore part of the booking
payload, not independently writable resources -- which also keeps the hierarchy at the depth
the existing guards enforce.

HTTP concerns only. The 404s, 409s and 422s are raised by the service and schemas and rendered
by the Stage 3A handlers.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.deps import BookingServiceDep, ReconciliationServiceDep, require_role
from app.models.enums import HotelRole
from app.schemas.booking import (
    BookingCreate,
    BookingResponse,
    BookingUpdate,
    StayExtension,
    StayModification,
    StayModificationResponse,
)
from app.schemas.common import ErrorResponse, Page
from app.schemas.reconciliation import BookingReconciliation
from app.services.booking import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

router = APIRouter(prefix="/hotels/{hotel_public_id}/bookings", tags=["bookings"])

HotelPath = Annotated[uuid.UUID, Path(description="Public identifier of the owning hotel.")]
BookingPath = Annotated[uuid.UUID, Path(description="Public identifier of the booking.")]

NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": "The hotel, the guest, a named room, or the booking does not exist.",
    }
}
CONFLICT_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_409_CONFLICT: {
        "model": ErrorResponse,
        "description": "A room is already booked for overlapping dates, the reference is "
        "taken, or the booking is still referenced by other records.",
    }
}


@router.post(
    "",
    response_model=BookingResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a booking with its rooms and nightly rates",
    description="Creates the booking, its room allocations and every priced night in one "
    "transaction. Room availability is decided by the database's exclusion constraint, not "
    "by a prior check.",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
    # Stage 4.2: staff or above. Declared here because which role a
    # verb needs is a fact about the HTTP surface, not about the hotel.
    dependencies=[Depends(require_role(HotelRole.STAFF))],
)
def create_booking(
    hotel_public_id: HotelPath, payload: BookingCreate, service: BookingServiceDep
) -> BookingResponse:
    """201 on success; 404 for an unknown hotel, guest or room; 409 when a room is already
    held for overlapping dates by an active booking."""
    return service.create(hotel_public_id, payload)


@router.get(
    "",
    response_model=Page[BookingResponse],
    summary="List a hotel's bookings",
    description="One page of bookings, most recent arrival date first.",
    responses=NOT_FOUND_RESPONSE,
)
def list_bookings(
    hotel_public_id: HotelPath,
    service: BookingServiceDep,
    page: int = Query(default=1, ge=1, description="1-based page number."),
    page_size: int = Query(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Rows per page (max {MAX_PAGE_SIZE}).",
    ),
) -> Page[BookingResponse]:
    """404 for an unknown hotel -- distinct from a known hotel with no bookings."""
    return service.list(hotel_public_id, page=page, page_size=page_size)


@router.get(
    "/{booking_public_id}",
    response_model=BookingResponse,
    summary="Get a booking",
    responses=NOT_FOUND_RESPONSE,
)
def get_booking(
    hotel_public_id: HotelPath, booking_public_id: BookingPath, service: BookingServiceDep
) -> BookingResponse:
    """Resolved by hotel AND public id, so another property's booking is unreachable."""
    return service.get(hotel_public_id, booking_public_id)


@router.get(
    "/{booking_public_id}/reconciliation",
    response_model=BookingReconciliation,
    summary="Reconcile a booking against its ledger",
    description=(
        "What the stay is worth by the server's own reckoning, what has been charged and "
        "refunded against it, and what remains. Derived at read time; nothing is stored."
    ),
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
)
def get_reconciliation(
    hotel_public_id: HotelPath,
    booking_public_id: BookingPath,
    service: ReconciliationServiceDep,
) -> BookingReconciliation:
    """A derived view of ONE booking, which is why it lives here and not under /analytics.

    Two architectural rules bracket this route and both were consulted rather than bent.
    `test_the_ledger_itself_exposes_no_aggregation_route` forbids totals on a ledger
    collection -- so this is not a field on `/payments`, which still returns only rows. And
    every `/analytics/` route requires an explicit date window, because a period report with
    an implicit one means different things on different days; a single booking has no window
    to ask for, so it does not belong in that domain either.

    A read, carrying the same access rule as reading the booking or its payments: membership
    of the hotel, no additional role.
    """
    return service.booking_reconciliation(hotel_public_id, booking_public_id)


@router.patch(
    "/{booking_public_id}",
    response_model=BookingResponse,
    summary="Partially update a booking",
    description="Status, occupancy, source and commercial fields only. Dates, guest, "
    "reference and room allocation are not editable here -- changing them is a re-pricing "
    "operation the deferred night-completeness trigger will not accept as a field edit.",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
    # Stage 4.2: staff or above. Declared here because which role a verb
    # needs is a fact about the HTTP surface, not about the hotel.
    dependencies=[Depends(require_role(HotelRole.STAFF))],
)
def update_booking(
    hotel_public_id: HotelPath,
    booking_public_id: BookingPath,
    payload: BookingUpdate,
    service: BookingServiceDep,
) -> BookingResponse:
    """Confirming a booking whose room has since been taken returns 409: the status change
    cascades to the allocations and re-triggers the exclusion constraint."""
    return service.update(hotel_public_id, booking_public_id, payload)


@router.patch(
    "/{booking_public_id}/stay",
    response_model=StayModificationResponse,
    summary="Move a booking's stay",
    description=(
        "Replace a booking's dates and room allocation in one transaction. The whole stay is "
        "restated, not patched: moving the dates rewrites every allocation and every priced "
        "night, so the payload carries all of them, in the same shape a booking is created "
        "with. Room availability is decided by the database's exclusion constraint. "
        "The nights are re-priced by the server and the response reports what that did to "
        "the money: whether anything is now owed, whether anything became refundable, or "
        "neither. Nothing is charged and nothing is refunded -- settling a difference is a "
        "separate act through the payment endpoints."
    ),
    dependencies=[Depends(require_role(HotelRole.STAFF))],
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
)
def modify_stay(
    hotel_public_id: HotelPath,
    booking_public_id: BookingPath,
    payload: StayModification,
    service: BookingServiceDep,
) -> StayModificationResponse:
    """A sub-resource, and the one kind this router is allowed to have.

    Allocations and nights get no routes of their own because the deferred completeness
    trigger means neither can be committed alone. This route does not break that rule -- it
    is the whole stay aggregate, written in a single transaction, which is precisely the unit
    the trigger judges.

    Carries STAFF, the same role that already guards creating and updating a booking.
    """
    return service.modify_stay(hotel_public_id, booking_public_id, payload)


@router.post(
    "/{booking_public_id}/stay/extension",
    response_model=StayModificationResponse,
    status_code=status.HTTP_200_OK,
    summary="Extend a checked-in guest's stay",
    description=(
        "Keeps an in-house guest longer by moving check-out outward. Only a booking that "
        "is checked_in may be extended; a pending or confirmed booking is served by the "
        "stay modification endpoint instead, which can restate the whole stay. "
        "The payload carries one field, and the fields it lacks are the contract: "
        "check-in cannot move, no room can be added, changed or released, the stay "
        "cannot be shortened, and the new check-out must be strictly later than the "
        "current one -- a request that repeats the current date is refused rather than "
        "reported as a successful no-op. "
        "The nights already slept keep the rates they were sold at; only the added "
        "nights are priced, by the server, from the room type's configured rate. No "
        "amount can be proposed. The extension is subject to availability: it succeeds "
        "only if the room is free for the added nights, decided by the database's "
        "exclusion constraint rather than by a prior check, and returns 409 otherwise. "
        "The response reports what the extension did to the money. Nothing is charged "
        "and nothing is refunded -- settling a difference is a separate act through the "
        "payment endpoints."
    ),
    dependencies=[Depends(require_role(HotelRole.STAFF))],
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
)
def extend_stay(
    hotel_public_id: HotelPath,
    booking_public_id: BookingPath,
    payload: StayExtension,
    service: BookingServiceDep,
) -> StayModificationResponse:
    """A route of its own, and the separation is the design.

    It would have been shorter to widen ``PATCH .../stay``: admit ``checked_in``, make
    ``check_in_date`` and ``rooms`` optional, and infer from the payload's shape which
    of two very different operations the caller meant. That is precisely the wrong
    trade. It would make the status policy depend on which fields a client happened to
    send, put "may this guest's room be reassigned?" behind an omitted key, and hand the
    replace-the-whole-stay operation a status it is documented to refuse -- so a client
    that forgot ``rooms`` on a checked-in booking would release the room the guest is
    asleep in.

    Two routes instead. Each names exactly what it may do, each carries its own status
    policy, and neither can be reached by accident from the other. They share the
    response model because a caller needs the same two things afterwards either way.

    POST rather than PATCH: this appends nights to a stay rather than restating a
    representation of it, and it is deliberately not idempotent -- extending twice
    extends twice, and a repeat of the SAME target date is refused with a 409 saying so.
    200 rather than 201 because no separately addressable resource is created.

    Carries STAFF, the same role that guards creating, updating and modifying a booking.
    Extending an in-house stay is front-desk work, and inventing a higher bar for it
    than for cancelling the same booking would be a policy nobody decided.
    """
    return service.extend_stay(hotel_public_id, booking_public_id, payload)


@router.delete(
    "/{booking_public_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a booking",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
    # Stage 4.2: manager or above. Declared here because which role a verb
    # needs is a fact about the HTTP surface, not about the hotel.
    dependencies=[Depends(require_role(HotelRole.MANAGER))],
)
def delete_booking(
    hotel_public_id: HotelPath, booking_public_id: BookingPath, service: BookingServiceDep
) -> None:
    """204 on success -- allocations and nights cascade away with it. 409 when payments,
    revenue or reviews still reference it; cancelling is the usual alternative."""
    service.delete(hotel_public_id, booking_public_id)
