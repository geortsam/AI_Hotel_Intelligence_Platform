"""Removing stored demand predictions: the retention purge and hotel deletion (Issue H8).

Stage 6.8 kept every prediction it served, and said retention would be decided "once the growth
rate is measured". H8 decided it, and this is the only module that deletes from
``demand_predictions``. It is a repository of its own rather than two more methods on
:class:`~app.repositories.ml_prediction.MlPredictionRepository` on purpose: that repository's
method set is pinned by the stages that read it, and the hotel service, which needs one method
here, has no business importing the accuracy protocol that one imports.

## Expiry is by target date, never by when the prediction was made

Every reader of this table -- the read API, accuracy, drift and forecast performance -- selects by
``target_date``. Accuracy then keeps, per ``(target_date, horizon, model_version)``, the
EARLIEST prediction. A purge by ``generated_at`` could delete a group's earliest row and keep a
later one, silently changing an accuracy result for a window that still has data. A purge by
``target_date`` removes whole groups: a target date is either kept with every prediction made
for it, or gone.

Within a batch the rows go newest first (``generated_at`` descending, then ``id`` descending), so
even a purge interrupted mid-group leaves that group's earliest prediction -- the one accuracy
would select -- until last.

**The cutoff is a date the caller computes**, in the hotel's own calendar (see
``app.services.demand_prediction_retention``): this module compares ``target_date`` with it and
knows nothing about clocks or time zones.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models.hotel import Hotel
from app.models.prediction import DemandPrediction


class MlPredictionRemovalRepository:
    """Deletes predictions. Never inserts, never updates, and never commits."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def hotels_with_predictions(self) -> Sequence[tuple[Hotel, dt.date]]:
        """Every hotel holding at least one prediction, with its oldest target date, in id order.

        One grouped scan of the ``(hotel_id, target_date)`` index. The hotel is returned whole
        because its own time zone decides its cutoff; the date lets the caller skip a hotel with
        nothing expired without issuing a DELETE for it.
        """
        rows = self._session.execute(
            select(Hotel, func.min(DemandPrediction.target_date))
            .join(DemandPrediction, DemandPrediction.hotel_id == Hotel.id)
            .group_by(Hotel.id)
            .order_by(Hotel.id)
        ).all()
        return [(hotel, oldest) for hotel, oldest in rows]

    def purge_expired(self, hotel_id: int, before: dt.date, *, limit: int) -> int:
        """Delete up to *limit* of one hotel's predictions whose target date is before *before*.

        Oldest target date first; within one, newest prediction first. Returns rows deleted.
        Does not commit: the caller owns the transaction, and a batch is one.
        """
        batch: Any = (
            select(DemandPrediction.id)
            .where(DemandPrediction.hotel_id == hotel_id, DemandPrediction.target_date < before)
            .order_by(
                DemandPrediction.target_date.asc(),
                DemandPrediction.generated_at.desc(),
                DemandPrediction.id.desc(),
            )
            .limit(limit)
            .scalar_subquery()
        )
        result = self._session.execute(
            delete(DemandPrediction).where(
                DemandPrediction.hotel_id == hotel_id, DemandPrediction.id.in_(batch)
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)

    def delete_for_hotel(self, hotel_id: int) -> None:
        """Remove every prediction of a hotel that is about to be deleted.

        The same arrangement as ``MembershipRepository.delete_for_hotel``: called by the hotel
        service inside its delete transaction, before the hotel row goes. If anything else still
        refuses the delete, that transaction rolls back and every prediction removed here is
        restored. ``fk_demand_predictions_hotel_id_hotels`` stays ``ON DELETE RESTRICT``, so a
        direct ``DELETE FROM hotels`` is still refused while predictions exist.
        """
        self._session.execute(delete(DemandPrediction).where(DemandPrediction.hotel_id == hotel_id))
        self._session.flush()


__all__ = ["MlPredictionRemovalRepository"]
