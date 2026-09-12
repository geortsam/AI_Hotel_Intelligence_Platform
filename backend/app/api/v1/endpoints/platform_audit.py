"""Platform-scoped audit history.

``/platform/audit-events``. Stage 4.5.13.

Stage 4.5.12 recorded eighteen kinds of change and could report eight of them. The other ten
belong to no property -- a password change, and the nine mutations of the three global
catalogues -- so ``audit_events.hotel_id`` is NULL for them and the hotel-scoped endpoint has
no hotel to ask under. This is where they are read.

**This is not a tenant endpoint, and nothing about it is hotel-shaped.** There is no hotel in
the path, no hotel in the response, no membership lookup, no role requirement, and no scope
resolver anywhere beneath it. A static test asserts the word does not appear in this module at
all. The security boundary is one line:

    authenticated caller -> require_platform_admin -> hotel_id IS NULL

and the last step is a database predicate, not a filter applied to rows that were fetched
anyway.

**Platform authority is the whole authorization, and hotel membership is irrelevant to it.**
An owner at every property in the portfolio is refused; a platform administrator who belongs to
no property at all is admitted. That asymmetry is Stage 4.3's design -- a role is a per-hotel
grant and cannot confer authority over rows that belong to no hotel -- and this route declares
it exactly as the catalogue writes do, with ``require_platform_admin`` on the route.

**One verb.** No POST, no PATCH, no PUT, no DELETE, on the collection or anywhere else. An
audit trail a client can append to is a trail a client can forge, and one a client can edit is
not evidence. Migration 0007's trigger means nothing else can edit it either.

HTTP concerns only. The 401s, 403s and 422s are raised by the shared dependency and schemas and
rendered by the Stage 3A handlers.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import PlatformAuditServiceDep, require_platform_admin
from app.models.enums import AuditAction, AuditResourceType
from app.schemas.audit import PlatformAuditEventResponse
from app.schemas.common import ErrorResponse, Page
from app.services.audit import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

router = APIRouter(
    prefix="/platform/audit-events",
    tags=["audit"],
    # Stage 4.3's dependency, unchanged and unwrapped. Depending on it -- which depends on
    # PlatformAccessPolicy, which depends on CurrentUserDep -- is what keeps an anonymous
    # caller on 401 rather than 403: a stranger is told to authenticate, and an authenticated
    # caller without the grant is told what they lack.
    dependencies=[Depends(require_platform_admin)],
)

RequestIdQuery = Annotated[
    str | None,
    Query(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._-]+$",
        description="Only events caused by this request, correlating them with the logs.",
    ),
]

FORBIDDEN_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_403_FORBIDDEN: {
        "model": ErrorResponse,
        "description": "The caller is authenticated but holds no platform administrator grant.",
    }
}


@router.get(
    "",
    response_model=Page[PlatformAuditEventResponse],
    summary="List platform-scoped audit history",
    description=(
        "Security-significant changes that belong to no property: password changes, and "
        "mutations of the amenity, revenue-category and expense-category catalogues that "
        "every hotel shares. Newest first. Read-only -- there is no endpoint that writes, "
        "edits or deletes an event. Requires a platform administrator grant; no hotel role "
        "grants access, and no hotel membership is consulted."
    ),
    responses=FORBIDDEN_RESPONSE,
)
def list_platform_audit_events(
    service: PlatformAuditServiceDep,
    page: int = Query(default=1, ge=1, description="1-based page number."),
    page_size: int = Query(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Rows per page (max {MAX_PAGE_SIZE}).",
    ),
    action: AuditAction | None = Query(
        default=None,
        description=(
            "Only this action. Validated against the closed audit vocabulary. An action that "
            "only ever occurs at a property returns an empty page rather than an error, so "
            "the two audit surfaces agree about what a valid filter is."
        ),
    ),
    resource_type: AuditResourceType | None = Query(
        default=None, description="Only events about this kind of resource."
    ),
    actor_public_id: uuid.UUID | None = Query(
        default=None,
        description=(
            "Only events by this account, named by its public identifier. An account that "
            "does not exist returns an empty page rather than an error, so this filter "
            "cannot be used to discover which accounts exist."
        ),
    ),
    request_id: RequestIdQuery = None,
    occurred_from: dt.datetime | None = Query(
        default=None, description="Only events at or after this moment (inclusive)."
    ),
    occurred_to: dt.datetime | None = Query(
        default=None, description="Only events at or before this moment (inclusive)."
    ),
) -> Page[PlatformAuditEventResponse]:
    """401 without a token; 403 for any authenticated caller without the platform grant.

    Every filter is the hotel surface's, with the same bounds and the same validation, so an
    operator moving between the two does not have to learn a second set of rules. None of them
    names an internal key, and none of them can widen the scope: the scope is not a parameter.
    """
    return service.list(
        page=page,
        page_size=page_size,
        action=action,
        resource_type=resource_type,
        actor_public_id=actor_public_id,
        request_id=request_id,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
    )
