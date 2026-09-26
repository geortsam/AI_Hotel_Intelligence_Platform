"""The attention list (Stage 7.12): ``GET /hotels/{hotel_public_id}/intelligence/priorities``.

A separate module under the same ``/intelligence`` prefix, so the V1 intelligence router is left
exactly as it was. Read-only and hotel-scoped: any member may read it; it computes on request,
stores nothing and calls no external service. Like every intelligence route, it observes and
explains -- it changes no rate, moves no booking, allocates no room and schedules nothing.

The design, the templates and the offline ranking protocol are in `docs/attention-list.md`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, status

from app.api.deps import InsightServiceDep
from app.schemas.common import ErrorResponse
from app.schemas.insight import PrioritiesResponse

router = APIRouter(prefix="/hotels/{hotel_public_id}/intelligence", tags=["intelligence"])

HotelPath = Annotated[uuid.UUID, Path(description="Public identifier of the hotel.")]
WindowFrom = Annotated[dt.date, Query(description="First observed day, inclusive.")]
WindowTo = Annotated[dt.date, Query(description="Last observed day, inclusive.")]

RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": ErrorResponse,
        "description": "No such hotel, or the caller is not a member of it. Indistinguishable.",
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": "The window is inverted or longer than the maximum observation span.",
    },
}


@router.get(
    "/priorities",
    response_model=PrioritiesResponse,
    summary="The attention list: a ranked, deterministic reading of this hotel's data",
    description=(
        "Upcoming peak days (the busiest 3 of the 14 days after the window, by the seasonal "
        "day-of-week forecast over the 90 days before them), then the strongest anomalies "
        "observed in the window, then the booking-demand trend when it moved. Every item states "
        "its measure, its figures and the service each came from, a comparison and a "
        "limitation, and points to the existing view where the data can be read. Sentences are "
        "fixed templates filled from those figures -- no generated text and no advice. How the "
        "day ranking compares with a simpler one is measured offline under insight_ranking_v1."
    ),
    responses=RESPONSES,
)
def get_priorities(
    hotel_public_id: HotelPath,
    service: InsightServiceDep,
    date_from: WindowFrom,
    date_to: WindowTo,
) -> PrioritiesResponse:
    return service.priorities(hotel_public_id, date_from, date_to)
