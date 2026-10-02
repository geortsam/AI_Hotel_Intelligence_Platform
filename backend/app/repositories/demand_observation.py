"""The declared observation spans of a hotel's demand (migration 0016).

One module owns ``demand_observation_periods``: every extractor that has to tell an observed zero
from an unknown day -- the Stage 6.1 dataset, model serving and the intelligence series -- reads the
spans here, and the operator job writes them here. A second query for the same table elsewhere
would be a second answer to "which dates of this hotel are observed?".

Nothing here commits, rolls back or decides what a conflict means: the declaring service owns the
transaction and translates what the database refuses.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.demand_observation import DemandObservationPeriod


class DemandObservationRepository:
    """Read and write one hotel's declared spans."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def periods(self, hotel_id: int) -> list[tuple[dt.date, dt.date]]:
        """Every declared span of one hotel, as ``(observed_from, observed_to)``, ascending."""
        rows = (
            self._session.execute(
                select(DemandObservationPeriod.observed_from, DemandObservationPeriod.observed_to)
                .where(DemandObservationPeriod.hotel_id == hotel_id)
                .order_by(DemandObservationPeriod.observed_from)
            )
            .tuples()
            .all()
        )
        return [(observed_from, observed_to) for observed_from, observed_to in rows]

    def add(self, hotel_id: int, observed_from: dt.date, observed_to: dt.date) -> None:
        """Stage one span and flush, so an overlap or a reversed span is refused here and now."""
        self._session.add(
            DemandObservationPeriod(
                hotel_id=hotel_id, observed_from=observed_from, observed_to=observed_to
            )
        )
        self._session.flush()

    def remove(self, hotel_id: int, observed_from: dt.date, observed_to: dt.date) -> int:
        """Delete the span with exactly these bounds. Returns how many rows went (0 or 1)."""
        result = self._session.execute(
            delete(DemandObservationPeriod).where(
                DemandObservationPeriod.hotel_id == hotel_id,
                DemandObservationPeriod.observed_from == observed_from,
                DemandObservationPeriod.observed_to == observed_to,
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)


__all__ = ["DemandObservationRepository"]
