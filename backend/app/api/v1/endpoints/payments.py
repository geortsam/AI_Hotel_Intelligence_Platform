"""Payment endpoints, nested under the booking they settle.

``/hotels/{hotel_public_id}/bookings/{booking_public_id}/payments``.

**Append-only: there is no PATCH and no DELETE, and neither should be added.** A payment is a
financial record; a mistake is corrected by posting a refund, which is what
``POST .../payments/refunds`` is for. The absence of those verbs is the API contract, not an
omission, and a test asserts it.

Charges and refunds are separate endpoints rather than one endpoint with a ``kind`` field.
``ck_payments_refund_references_charge`` is a biconditional -- a refund must name its parent
and a charge must not -- so a single endpoint would have to accept a payload that can express
an invalid combination and then reject it. Two endpoints cannot.

``payments.id`` never appears in a URL or a response; ``public_id`` (migration 0002) is the
identity throughout.

HTTP concerns only.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.deps import PaymentServiceDep, require_role
from app.models.enums import HotelRole
from app.schemas.common import ErrorResponse, Page
from app.schemas.payment import ChargeCreate, PaymentResponse, RefundCreate
from app.services.payment import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

router = APIRouter(
    prefix="/hotels/{hotel_public_id}/bookings/{booking_public_id}/payments",
    tags=["payments"],
)

HotelPath = Annotated[uuid.UUID, Path(description="Public identifier of the owning hotel.")]
BookingPath = Annotated[uuid.UUID, Path(description="Public identifier of the booking.")]
PaymentPath = Annotated[uuid.UUID, Path(description="Public identifier of the payment.")]

NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": "The hotel, the booking, or the payment does not exist.",
    }
}
CONFLICT_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_409_CONFLICT: {
        "model": ErrorResponse,
        "description": "The provider transaction reference has already been recorded, or the "
        "values violate a payment constraint.",
    }
}


@router.post(
    "",
    response_model=PaymentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Post a charge against a booking",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
    # Stage 4.2: staff or above. Declared here because which role a
    # verb needs is a fact about the HTTP surface, not about the hotel.
    dependencies=[Depends(require_role(HotelRole.STAFF))],
)
def create_charge(
    hotel_public_id: HotelPath,
    booking_public_id: BookingPath,
    payload: ChargeCreate,
    service: PaymentServiceDep,
) -> PaymentResponse:
    """201 on success; 404 if the hotel or booking is unknown; 409 if the provider reference
    has already been recorded -- payment providers deliver webhooks at least once, so a
    repeat must not create a second record."""
    return service.create_charge(hotel_public_id, booking_public_id, payload)


@router.post(
    "/refunds",
    response_model=PaymentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Post a refund against an existing payment",
    description="The payment being reversed is named by its public_id and must belong to the "
    "same booking.",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
    # Stage 4.2: staff or above. Declared here because which role a verb
    # needs is a fact about the HTTP surface, not about the hotel.
    dependencies=[Depends(require_role(HotelRole.STAFF))],
)
def create_refund(
    hotel_public_id: HotelPath,
    booking_public_id: BookingPath,
    payload: RefundCreate,
    service: PaymentServiceDep,
) -> PaymentResponse:
    """404 when the payment being refunded is not on this booking -- which is also what
    prevents this endpoint being used to probe for payments elsewhere."""
    return service.create_refund(hotel_public_id, booking_public_id, payload)


@router.get(
    "",
    response_model=Page[PaymentResponse],
    summary="List a booking's payments",
    description="One page of charges and refunds against this booking, oldest first.",
    responses=NOT_FOUND_RESPONSE,
)
def list_payments(
    hotel_public_id: HotelPath,
    booking_public_id: BookingPath,
    service: PaymentServiceDep,
    page: int = Query(default=1, ge=1, description="1-based page number."),
    page_size: int = Query(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Rows per page (max {MAX_PAGE_SIZE}).",
    ),
) -> Page[PaymentResponse]:
    """404 for an unknown hotel or booking -- distinct from a booking with no payments."""
    return service.list(hotel_public_id, booking_public_id, page=page, page_size=page_size)


@router.get(
    "/{payment_public_id}",
    response_model=PaymentResponse,
    summary="Get a payment",
    responses=NOT_FOUND_RESPONSE,
)
def get_payment(
    hotel_public_id: HotelPath,
    booking_public_id: BookingPath,
    payment_public_id: PaymentPath,
    service: PaymentServiceDep,
) -> PaymentResponse:
    """Resolved through hotel and booking, so another booking's payment is unreachable."""
    return service.get(hotel_public_id, booking_public_id, payment_public_id)
