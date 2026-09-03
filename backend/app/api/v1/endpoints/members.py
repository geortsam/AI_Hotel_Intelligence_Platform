"""Hotel membership endpoints, nested under their owning hotel.

``/hotels/{hotel_public_id}/members/{user_public_id}``. Membership is hotel-scoped even
though identity is not: the same person at two properties is one account and two memberships,
and the hotel segment is how one of them is reached.

**There is deliberately no flat route.** ``GET /users/{id}/memberships`` would answer a
question no hotel is entitled to ask -- it would tell one property's administrator which other
properties a colleague works at -- and it would sit outside the resolver that makes the 404
wall work. A structural test asserts no such route exists.

``users.id`` and ``user_hotels.id`` never appear in a URL or a response; the member is named
by the account's ``public_id``.

HTTP concerns only. The 404s, 403s and 409s are raised by the service and the policies as
domain errors and rendered by the Stage 3A handlers; this module builds no error payloads.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.deps import MembershipServiceDep, require_role
from app.models.enums import HotelRole
from app.schemas.common import ErrorResponse, Page
from app.schemas.membership import MemberCreate, MemberResponse, MemberUpdate
from app.services.membership import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

router = APIRouter(prefix="/hotels/{hotel_public_id}/members", tags=["members"])

HotelPath = Annotated[uuid.UUID, Path(description="Public identifier of the owning hotel.")]
MemberPath = Annotated[
    uuid.UUID, Path(description="Public identifier of the member's user account.")
]

NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": (
            "The hotel does not exist, the caller is not a member of it, or the named user "
            "is not a member of it. These are one response on purpose."
        ),
    }
}
FORBIDDEN_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_403_FORBIDDEN: {
        "model": ErrorResponse,
        "description": "The caller is a member of this hotel but holds too low a role.",
    }
}
CONFLICT_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_409_CONFLICT: {
        "model": ErrorResponse,
        "description": (
            "The user is already a member, or the change would leave the hotel with no owner."
        ),
    }
}


@router.get(
    "",
    response_model=Page[MemberResponse],
    summary="List the members of a hotel",
    description="Alphabetical by email. Requires the manager role: running a property means "
    "knowing who has access to it.",
    responses={**NOT_FOUND_RESPONSE, **FORBIDDEN_RESPONSE},
    dependencies=[Depends(require_role(HotelRole.MANAGER))],
)
def list_members(
    hotel_public_id: HotelPath,
    service: MembershipServiceDep,
    page: int = Query(default=1, ge=1, description="1-based page number."),
    page_size: int = Query(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Rows per page (max {MAX_PAGE_SIZE}).",
    ),
) -> Page[MemberResponse]:
    return service.list(hotel_public_id, page=page, page_size=page_size)


@router.post(
    "",
    response_model=MemberResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add an existing account to a hotel",
    description="Names an existing account by email; never creates one. Requires the owner "
    "role, because granting access to a property is the owner's decision.",
    responses={**NOT_FOUND_RESPONSE, **FORBIDDEN_RESPONSE, **CONFLICT_RESPONSE},
    dependencies=[Depends(require_role(HotelRole.OWNER))],
)
def add_member(
    hotel_public_id: HotelPath, payload: MemberCreate, service: MembershipServiceDep
) -> MemberResponse:
    return service.add(hotel_public_id, payload)


@router.patch(
    "/{user_public_id}",
    response_model=MemberResponse,
    summary="Change a member's role",
    description="Refused with 409 when it would demote the hotel's last owner.",
    responses={**NOT_FOUND_RESPONSE, **FORBIDDEN_RESPONSE, **CONFLICT_RESPONSE},
    dependencies=[Depends(require_role(HotelRole.OWNER))],
)
def change_member_role(
    hotel_public_id: HotelPath,
    user_public_id: MemberPath,
    payload: MemberUpdate,
    service: MembershipServiceDep,
) -> MemberResponse:
    return service.change_role(hotel_public_id, user_public_id, payload)


@router.delete(
    "/{user_public_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a member from a hotel",
    description="Revokes access by deleting the membership. Refused with 409 when it would "
    "remove the hotel's last owner. The account itself is untouched.",
    responses={**NOT_FOUND_RESPONSE, **FORBIDDEN_RESPONSE, **CONFLICT_RESPONSE},
    dependencies=[Depends(require_role(HotelRole.OWNER))],
)
def remove_member(
    hotel_public_id: HotelPath, user_public_id: MemberPath, service: MembershipServiceDep
) -> None:
    service.remove(hotel_public_id, user_public_id)
