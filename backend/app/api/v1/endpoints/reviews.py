"""Review endpoints.

The URL hierarchy is dictated by the frozen schema, which gives ``reviews`` no ``public_id``:

* ``GET /hotels/{hotel_public_id}/reviews`` -- the hotel's reviews, newest first. Every
  review is visible here, including ones harvested from an external platform that carry no
  booking and therefore have no individual URL.
* ``.../bookings/{booking_public_id}/review`` -- the review **of one stay**, addressed by its
  booking. Singular, because ``uq_reviews_booking_id`` makes it a singleton: the URL states
  the constraint rather than implying a collection that can only ever hold one row.

**There is no DELETE.** ``is_published`` is the column the schema provides for taking a
review down, and it is ``NOT NULL DEFAULT true`` -- a first-class part of the table, not an
afterthought. Offering deletion beside it would give two ways to remove a review with
different consequences for every aggregate computed over the table. PATCH with
``{"is_published": false}`` is the withdrawal.

PATCH reaches ``is_published`` and ``responded_at`` only. The rating, title, body and
reviewer name are the guest's account of their stay.

No internal BIGINT appears in any path or payload.

HTTP concerns only.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.deps import ReviewServiceDep, require_role
from app.models.enums import HotelRole
from app.schemas.common import ErrorResponse, Page
from app.schemas.review import (
    ReviewCreate,
    ReviewModerationUpdate,
    ReviewResponse,
    ReviewSourceLiteral,
)
from app.services.review import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

router = APIRouter(prefix="/hotels/{hotel_public_id}", tags=["reviews"])

HotelPath = Annotated[uuid.UUID, Path(description="Public identifier of the owning hotel.")]
BookingPath = Annotated[uuid.UUID, Path(description="Public identifier of the reviewed stay.")]

NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": "The hotel, the booking, or the booking's review does not exist.",
    }
}
CONFLICT_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_409_CONFLICT: {
        "model": ErrorResponse,
        "description": "The stay already has a review, or the values violate a constraint.",
    }
}


@router.get(
    "/reviews",
    response_model=Page[ReviewResponse],
    summary="List a hotel's reviews",
    description="Newest first, matching the direction of ix_reviews_hotel_id_review_date.",
    responses=NOT_FOUND_RESPONSE,
)
def list_reviews(
    hotel_public_id: HotelPath,
    service: ReviewServiceDep,
    source: ReviewSourceLiteral | None = Query(
        default=None, description="Restrict to one channel. Backed by ix_reviews_hotel_id_source."
    ),
    is_published: bool | None = Query(
        default=None, description="Restrict to published or hidden reviews."
    ),
    page: int = Query(default=1, ge=1, description="1-based page number."),
    page_size: int = Query(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Rows per page (max {MAX_PAGE_SIZE}).",
    ),
) -> Page[ReviewResponse]:
    """404 for an unknown hotel -- distinct from a hotel with no reviews, which is an empty
    page."""
    return service.list(
        hotel_public_id,
        page=page,
        page_size=page_size,
        source=source,
        is_published=is_published,
    )


@router.post(
    "/bookings/{booking_public_id}/review",
    response_model=ReviewResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record the review of a stay",
    description="The author is the booking's guest; it is not accepted in the payload.",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
    # Stage 4.2: staff or above. Declared here because which role a
    # verb needs is a fact about the HTTP surface, not about the hotel.
    dependencies=[Depends(require_role(HotelRole.STAFF))],
)
def create_review(
    hotel_public_id: HotelPath,
    booking_public_id: BookingPath,
    payload: ReviewCreate,
    service: ReviewServiceDep,
) -> ReviewResponse:
    """201 on success; 404 if the hotel or booking is unknown; 409 if the stay has already
    been reviewed -- ``uq_reviews_booking_id`` decides that, not a prior lookup."""
    return service.create(hotel_public_id, booking_public_id, payload)


@router.get(
    "/bookings/{booking_public_id}/review",
    response_model=ReviewResponse,
    summary="Get the review of a stay",
    responses=NOT_FOUND_RESPONSE,
)
def get_review(
    hotel_public_id: HotelPath,
    booking_public_id: BookingPath,
    service: ReviewServiceDep,
) -> ReviewResponse:
    """Resolved through hotel and booking, so another property's review is unreachable."""
    return service.get(hotel_public_id, booking_public_id)


@router.patch(
    "/bookings/{booking_public_id}/review",
    response_model=ReviewResponse,
    summary="Publish, hide, or mark a review answered",
    description="Accepts is_published and responded_at only. Omitted fields are untouched; "
    "an explicit null clears the column.",
    responses={**NOT_FOUND_RESPONSE, **CONFLICT_RESPONSE},
    # Stage 4.2: staff or above. Declared here because which role a verb
    # needs is a fact about the HTTP surface, not about the hotel.
    dependencies=[Depends(require_role(HotelRole.STAFF))],
)
def moderate_review(
    hotel_public_id: HotelPath,
    booking_public_id: BookingPath,
    payload: ReviewModerationUpdate,
    service: ReviewServiceDep,
) -> ReviewResponse:
    """The guest's own words are not reachable from here, by design."""
    return service.moderate(hotel_public_id, booking_public_id, payload)
