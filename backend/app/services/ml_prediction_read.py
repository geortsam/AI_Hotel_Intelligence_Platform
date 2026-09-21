"""Reading a hotel's own stored demand predictions. Reads only, writes nothing.

Stage 6.11. Stage 6.8 made every served prediction durable, and Stages 6.9 and 6.10 built two
readers over those rows -- both programmatic, both invoked only by tests. The hotel whose
business data the rows are had no path to a single one of them. This is that path, and it is
nothing else.

    HotelScopeResolver       who may reach this hotel, and which row it is   (app.services.scope)
        |
    MlPredictionRepository   one page of stored rows, plus the total         (one hotel, one window)
        |
    Page[StoredDemandPredictionResponse]

## Every row, deliberately

This service returns **all** stored predictions in the window. It does not collapse a target
date, does not prefer the earliest ``generated_at``, and does not pool model versions -- all of
which :meth:`MlPredictionRepository.scorable_predictions` does, correctly, for Stage 6.9.

Those semantics answer different questions. Accuracy asks *how far off were we*, where counting
one target date twice would be wrong. This asks *what were we told*, where dropping an answer we
gave would be hiding history -- and Stage 6.8 made repeat predictions genuinely distinct rows
rather than duplicates, because a booking recorded late changes the inputs. Two separate
repository methods keep the two rules from ever being one edit apart.

## Read-only, and structurally so

It holds **no session**. It cannot commit, roll back or flush because it has nothing to do those
things to, and it builds no SQLAlchemy query -- every ``select`` on this path lives in the
repository. The same arrangement as ``DemandAccuracyService`` and ``DemandDistributionService``.

## Tenant isolation

The hotel is resolved FIRST, through the shared resolver, before any prediction is read. An
unknown hotel and a hotel the caller is not a member of raise the same error with the same
message, so the endpoint cannot be used to discover which properties exist. The read is then
bounded by the internal ``hotel_id`` the resolver returned -- never by anything a caller
supplied -- and that id never leaves this process.

## Bounded by construction

Both dates are required and ``page_size`` is capped, so there is no request that asks for an
unbounded result. ``date_from`` and ``date_to`` are inclusive, matching the audit history
endpoint's ``occurred_from``/``occurred_to``.

## No clock

Every bound is a parameter. Nothing here calls ``now``, ``today`` or ``utcnow``, so the same
request over unchanged rows returns the same page whenever it is made.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import uuid

from app.core.errors import ValidationError
from app.ml.serving import APPROVED_MODEL
from app.repositories.ml_prediction import MlPredictionRepository
from app.schemas.common import Page
from app.schemas.ml_prediction_read import StoredDemandPredictionResponse
from app.services.scope import HotelScopeResolver

logger = logging.getLogger(__name__)

#: The page conventions this domain uses, mirroring `app.services.audit` and the other paginated
#: histories. Declared here rather than imported, as each of those declares its own.
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20

#: The outcomes a read can report. Two, because a request either found rows or found none -- and
#: finding none is a normal answer for a window nothing was served in, not a failure.
RETURNED = "returned"
EMPTY = "empty"


class DemandPredictionReadService:
    """One hotel, one window, one page. Deliberately nothing else.

    No method that reads across hotels and no parameter through which a second could be named,
    for the reason every ML repository method in this codebase takes one hotel at a time.
    """

    def __init__(
        self, predictions: MlPredictionRepository, scope: HotelScopeResolver
    ) -> None:
        self._predictions = predictions
        self._scope = scope

    def list_predictions(
        self,
        hotel_public_id: uuid.UUID,
        *,
        date_from: dt.date,
        date_to: dt.date,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Page[StoredDemandPredictionResponse]:
        """One page of this hotel's stored predictions, newest-last within the window.

        Both dates are required and inclusive. The window is validated before the hotel is
        resolved only in the sense that a malformed request is a malformed request either way --
        but the *read* never happens before authorization, which is what matters.
        """
        # Authorization and tenancy first, always. Nothing below this line runs for a caller who
        # is not a member, so a non-member leaves no trace and learns nothing about this hotel.
        hotel = self._scope.require_hotel(hotel_public_id)

        started = time.perf_counter()
        self._require_window(date_from, date_to)
        self._require_pagination(page, page_size)

        rows, total = self._predictions.stored_predictions_page(
            hotel.id, date_from, date_to, page=page, page_size=page_size
        )
        items = [
            StoredDemandPredictionResponse(
                public_id=row.public_id,
                target_date=row.target_date,
                forecast_horizon_days=row.forecast_horizon_days,
                prediction_cutoff=row.prediction_cutoff,
                predicted_room_nights=row.predicted_room_nights,
                model_name=row.model_name,
                model_version=row.model_version,
                feature_version=row.feature_version,
                dataset_version=row.dataset_version,
                generated_at=row.generated_at,
            )
            for row in rows
        ]

        self._observe(len(items), time.perf_counter() - started)
        return Page.build(items=items, total=total, page=page, page_size=page_size)

    @staticmethod
    def _require_window(date_from: dt.date, date_to: dt.date) -> None:
        """A window that ends before it starts is malformed, not empty.

        Returning an empty page would let a client mistake a typo for a quiet period, which is
        the kind of silence that gets read as data.
        """
        if date_from > date_to:
            raise ValidationError("date_from must not be later than date_to.")

    @staticmethod
    def _require_pagination(page: int, page_size: int) -> None:
        """Bounded at the service too, not only at the route.

        The router declares the same bounds, so a real HTTP caller is refused before reaching
        here. This exists because the service is also callable programmatically, and an
        unbounded ``page_size`` arriving that way would be an unbounded query.
        """
        if page < 1:
            raise ValidationError("page must be 1 or greater.")
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValidationError(f"page_size must be between 1 and {MAX_PAGE_SIZE}.")

    def _observe(self, returned: int, seconds: float) -> None:
        """One event per request. Four fields, and nothing a hotel owns.

        No prediction value, no feature value, no hotel identifier of either kind, no digest, no
        date window, no page contents, no path. The count is a count; everything that would make
        it identifiable stays out.

        ``model_version`` is the approved model's label, already published in the model card.
        The request id is not passed: ``RequestIdFilter`` attaches it already.
        """
        outcome = RETURNED if returned else EMPTY
        logger.info(
            "stored demand predictions %s (model=%s): %s row(s) in %.1f ms",
            outcome,
            APPROVED_MODEL.model_version,
            returned,
            seconds * 1000,
            extra={
                "outcome": outcome,
                "model_version": APPROVED_MODEL.model_version,
                "predictions_returned": returned,
                "duration_ms": round(seconds * 1000, 3),
            },
        )


__all__ = ["DEFAULT_PAGE_SIZE", "EMPTY", "MAX_PAGE_SIZE", "RETURNED", "DemandPredictionReadService"]
