"""Declaring which dates of a hotel's demand are observed (migration 0016).

A declaration says: for this hotel, every date from ``observed_from`` to ``observed_to``, both
inclusive, is represented in full by the booking tables. It is the only evidence of observation
the platform accepts -- see :class:`app.ml.dataset.ObservationPeriod` -- so inside a declared span
a date with no occupied nights is a zero, and outside every span a date's demand is unknown.

**An operator's act, not a hotel member's.** It is reached through
``python -m app.jobs.demand_observation``, on the precedent of the conversation purge and the
audit archival: a function with a session, no route, no actor. A claim about the completeness of
a hotel's record is a statement about how the platform was operated, which no booking or
membership row can establish, and it moves what the model and the forecasts are told. No audit
event is written, for the reason ``app.services.retention`` gives: the job acts for nobody, and
inventing a system actor would change the actor model rather than use it.

**What it refuses.**

* a span that ends before it starts;
* a span reaching the hotel's own today or later. A day that has not ended cannot have a
  complete record, and a declared future day would become an observed zero the moment anything
  read it. "Today" is the hotel's calendar date (``hotels.timezone``, the zone its stay dates are
  kept in), or UTC's when the stored zone is not one the server knows;
* a span sharing a date with one already declared (the database's exclusion constraint), so
  "is this date observed?" keeps one answer. Re-declaring an identical span changes nothing and
  is reported as such, so a repeated run is harmless.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import (
    GENERIC_CONFLICT_MESSAGE,
    ConflictError,
    NotFoundError,
    ValidationError,
    constraint_name_of,
)
from app.ml.dataset import ObservationPeriod
from app.models.hotel import Hotel
from app.repositories.demand_observation import DemandObservationRepository
from app.repositories.hotel import HotelRepository

#: The exclusion constraint the database refuses an overlapping span with.
OVERLAP_CONSTRAINT = "excl_demand_observation_periods_no_overlap"

Clock = Callable[[], dt.datetime]


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class DemandObservationService:
    """Declare, withdraw and list one hotel's observation spans. Owns the transaction."""

    def __init__(
        self,
        session: Session,
        hotels: HotelRepository,
        periods: DemandObservationRepository,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._session = session
        self._hotels = hotels
        self._periods = periods
        self._clock = clock or _utc_now

    def spans(self, hotel_public_id: uuid.UUID) -> list[ObservationPeriod]:
        """The hotel's declared spans, ascending."""
        hotel = self._require_hotel(hotel_public_id)
        return [ObservationPeriod(a, b) for a, b in self._periods.periods(hotel.id)]

    def declare(
        self, hotel_public_id: uuid.UUID, observed_from: dt.date, observed_to: dt.date
    ) -> bool:
        """Record one span. Returns False when exactly this span was already declared."""
        hotel = self._require_hotel(hotel_public_id)
        if observed_to < observed_from:
            raise ValidationError("An observation period cannot end before it starts.")
        today = self._today(hotel)
        if observed_to >= today:
            raise ValidationError(
                f"An observation period must end before the hotel's today ({today.isoformat()}): "
                "a day that has not ended cannot have a complete record."
            )
        if (observed_from, observed_to) in self._periods.periods(hotel.id):
            return False
        try:
            self._periods.add(hotel.id, observed_from, observed_to)
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            if constraint_name_of(exc) == OVERLAP_CONSTRAINT:
                raise ConflictError(
                    "That period shares a date with one already declared for this hotel."
                ) from exc
            raise ConflictError(GENERIC_CONFLICT_MESSAGE) from exc
        return True

    def withdraw(
        self, hotel_public_id: uuid.UUID, observed_from: dt.date, observed_to: dt.date
    ) -> None:
        """Remove the span with exactly these bounds; its dates become unknown again."""
        hotel = self._require_hotel(hotel_public_id)
        if self._periods.remove(hotel.id, observed_from, observed_to) == 0:
            self._session.rollback()
            raise NotFoundError("No observation period with those dates is declared.")
        self._session.commit()

    def _require_hotel(self, hotel_public_id: uuid.UUID) -> Hotel:
        hotel = self._hotels.get_by_public_id(hotel_public_id)
        if hotel is None:
            raise NotFoundError("Hotel not found.")
        return hotel

    def _today(self, hotel: Hotel) -> dt.date:
        """The hotel's calendar date now, in its own zone; UTC's when the zone is unknown."""
        try:
            zone = ZoneInfo(hotel.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            zone = ZoneInfo("UTC")
        return self._clock().astimezone(zone).date()


__all__ = ["OVERLAP_CONSTRAINT", "DemandObservationService"]
