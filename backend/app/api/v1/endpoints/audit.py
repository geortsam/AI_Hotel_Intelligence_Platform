"""Audit-history endpoint, nested under the hotel whose history it is.

``/hotels/{hotel_public_id}/audit-events``.

**One verb, and the missing ones are the contract.** There is no POST, no PATCH, no PUT and no
DELETE -- not on a collection and not on an event. An audit trail a client can append to is a
trail a client can forge; one a client can edit is not evidence at all. The application offers
no way to do either, and migration 0007 installs a trigger so that nothing else can either.

Tenant isolation is the hotel segment plus the membership behind it, exactly as everywhere
else: the service resolves the hotel through ``HotelScopeResolver``, so a property the caller
does not belong to is *not found* rather than forbidden.

HTTP concerns only. The 404s and 422s are raised by the service and schemas and rendered by
the Stage 3A handlers.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, status

from app.api.deps import AuditServiceDep
from app.models.enums import AuditAction, AuditResourceType
from app.schemas.audit import AuditEventResponse
from app.schemas.common import ErrorResponse, Page
from app.services.audit import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

router = APIRouter(prefix="/hotels/{hotel_public_id}/audit-events", tags=["audit"])

HotelPath = Annotated[uuid.UUID, Path(description="Public identifier of the owning hotel.")]

NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": "The hotel does not exist, or the caller is not a member of it.",
    }
}
FORBIDDEN_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_403_FORBIDDEN: {
        "model": ErrorResponse,
        "description": "The caller is a member but holds a role below manager.",
    }
}
UNPROCESSABLE_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": (
            "A query parameter is malformed, or the occurrence window ends before it begins."
        ),
    }
}


@router.get(
    "",
    response_model=Page[AuditEventResponse],
    summary="List a hotel's audit history",
    description=(
        "Security- and business-significant changes at this property, newest first: who did "
        "what, to which resource, when, and under which request id. Read-only -- there is no "
        "endpoint that writes, edits or deletes an event. Requires the manager role, the "
        "same level the member listing requires."
    ),
    responses={**NOT_FOUND_RESPONSE, **FORBIDDEN_RESPONSE, **UNPROCESSABLE_RESPONSE},
)
def list_audit_events(
    hotel_public_id: HotelPath,
    service: AuditServiceDep,
    page: int = Query(default=1, ge=1, description="1-based page number."),
    page_size: int = Query(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Rows per page (max {MAX_PAGE_SIZE}).",
    ),
    action: AuditAction | None = Query(
        default=None,
        description="Only this action. Validated against the closed audit vocabulary.",
    ),
    resource_type: AuditResourceType | None = Query(
        default=None, description="Only events about this kind of resource."
    ),
    resource_reference: str | None = Query(
        default=None,
        min_length=1,
        max_length=64,
        description=(
            "Only events about the one resource named by this reference -- the public "
            "identifier or catalogue code the response itself reports. Matched exactly, "
            "and best paired with resource_type, since a reference is only unique within "
            "its kind. A reference that names nothing returns an empty page rather than "
            "an error."
        ),
    ),
    actor_public_id: uuid.UUID | None = Query(
        default=None,
        description=(
            "Only events by this account, named by the public identifier the member listing "
            "shows. An account that does not exist returns an empty page rather than an "
            "error, so this filter cannot be used to discover which accounts exist."
        ),
    ),
    request_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._-]+$",
        description="Only events caused by this request, correlating them with the logs.",
    ),
    occurred_from: dt.datetime | None = Query(
        default=None, description="Only events at or after this moment (inclusive)."
    ),
    occurred_to: dt.datetime | None = Query(
        default=None, description="Only events at or before this moment (inclusive)."
    ),
) -> Page[AuditEventResponse]:
    """404 for an unknown hotel or a non-member; 403 for a member below manager.

    Every filter is expressed in values a client already legitimately holds -- a public id, a
    vocabulary term, a request id it was handed in a response header. None of them names an
    internal key, and none of them widens the tenant scope the hotel segment establishes.
    """
    return service.list(
        hotel_public_id,
        page=page,
        page_size=page_size,
        action=action,
        resource_type=resource_type,
        resource_reference=resource_reference,
        actor_public_id=actor_public_id,
        request_id=request_id,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
    )
