"""The retention of `demand_predictions`: what expires, and the job that deletes it (Issue H8).

## The contract

A stored prediction is kept while its ``target_date`` is no more than
`demand_prediction_retention_days` (730 by default) before its hotel's own calendar date today.
After that it is **deleted**, physically, by :func:`purge_expired_predictions`; nothing is
archived.

Precisely: at the instant the purge starts, each hotel's today is taken in its own time zone
(UTC if the zone is unknown -- :func:`app.services.business_day.hotel_today`, the rule every other
"today" in this codebase uses), its cutoff is ``today - retention_days``, and every prediction with
``target_date < cutoff`` is deleted. The cutoff day itself is kept. With 730 days on 2026-10-09,
target dates up to 2024-10-08 go and 2024-10-09 stays. The purge reads the clock once, so every
hotel is judged at the same instant.

**Never by `generated_at`.** See ``app.repositories.ml_prediction_removal``: a target date is kept
with every prediction made for it or removed with all of them, so accuracy over any retained
window selects exactly the prediction it selected before the purge.

## What a purged window looks like

Empty. The read API returns an empty page, accuracy and drift find nothing to score or
summarise, and a forecast-performance window or baseline older than the retention period has no
predictions in it. Nothing distinguishes "purged" from "never served" -- 730 days keeps a 366-day
window and a baseline a year before it inside the period; a longer look-back needs a longer
retention, set before the rows are purged.

## What the job does, and does not

* **One hotel at a time, in bounded batches.** Every DELETE is scoped to one hotel's id and to at
  most `batch_size` of its expired predictions, and runs in its own transaction: a failure leaves
  every earlier batch deleted and nothing half-done, and running the job again finishes the work.
* **Counts only.** Its result and its log line say how many predictions, hotels and batches, and
  the retention -- never a hotel, a prediction, a value or a date.
* **No actor, no audit event, no model**, for the reasons ``app.services.retention`` gives for
  audit archival: the job acts for nobody. It never loads the model and never touches any other
  table.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.repositories.ml_prediction_removal import MlPredictionRemovalRepository
from app.services.business_day import Clock, hotel_today, utc_now

logger = logging.getLogger(__name__)

#: The most predictions one batch deletes: the bound the other purges use.
PURGE_BATCH = 1_000


@dataclass(frozen=True, slots=True)
class PredictionPurgeResult:
    """What one purge did, in counts only. No identifier, no hotel, no date."""

    #: Expired predictions physically deleted.
    predictions_deleted: int
    #: Hotels that held at least one expired prediction when the purge began.
    hotels: int
    #: Batches run: one transaction each, including each hotel's last, short one.
    batches: int
    #: The retention period the cutoffs were computed from, in calendar days.
    retention_days: int


def retention_cutoff(today: dt.date, retention_days: int) -> dt.date:
    """The oldest target date kept: everything strictly before it has expired."""
    return today - dt.timedelta(days=retention_days)


def purge_expired_predictions(
    session: Session,
    settings: Settings,
    *,
    batch_size: int = PURGE_BATCH,
    clock: Clock = utc_now,
) -> PredictionPurgeResult:
    """The job entry point: physically delete every expired prediction, at every hotel.

    **A function, not an endpoint**, on the precedent of ``purge_expired_invocations``: the
    ``app.jobs.purge_demand_predictions`` command, a scheduler or an operator with a Python shell
    calls it with a session and the application's settings.

    For each hotel whose oldest prediction is before its cutoff, in id order, batches run until
    one deletes fewer than ``batch_size``. Running it again is safe: a second run on the same day
    finds nothing and deletes nothing. ``clock`` is injectable so a test can stand at any instant.
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be at least 1, got {batch_size}")
    retention_days = settings.demand_prediction_retention_days
    repository = MlPredictionRemovalRepository(session)
    now = clock()

    def at_start() -> dt.datetime:
        return now

    try:
        # Each cutoff is computed here, while the hotels are loaded; the commit below ends the
        # read's transaction and every batch after it is its own.
        work = [
            (hotel.id, cutoff)
            for hotel, oldest in repository.hotels_with_predictions()
            if oldest < (cutoff := retention_cutoff(hotel_today(hotel, at_start), retention_days))
        ]
        session.commit()
    except Exception:
        session.rollback()
        raise

    deleted = 0
    batches = 0
    for hotel_id, cutoff in work:
        while True:
            try:
                purged = repository.purge_expired(hotel_id, cutoff, limit=batch_size)
                session.commit()
            except Exception:
                session.rollback()
                raise
            deleted += purged
            batches += 1
            if purged < batch_size:
                break

    logger.info(
        "demand prediction purge: %s deleted at %s hotel(s) in %s batch(es)",
        deleted,
        len(work),
        batches,
        extra={
            "demand_predictions_purged": deleted,
            "purge_hotels": len(work),
            "purge_batches": batches,
        },
    )
    return PredictionPurgeResult(
        predictions_deleted=deleted,
        hotels=len(work),
        batches=batches,
        retention_days=retention_days,
    )


__all__ = [
    "PURGE_BATCH",
    "PredictionPurgeResult",
    "purge_expired_predictions",
    "retention_cutoff",
]
