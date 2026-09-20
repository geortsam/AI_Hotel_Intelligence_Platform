"""Recording a served demand prediction. One statement, and the database decides.

Stage 6.8. Read-only everywhere else in this layer; this is the one ML repository that writes,
and like every other repository it **neither commits nor rolls back** -- the transaction
boundary belongs to the service that knows what a complete unit of work is.

## Why this is a single INSERT and not a lookup followed by one

The obvious shape is: select the row, and insert if it is not there. It is also wrong, and the
window between the two statements is the reason. Two identical requests arriving together would
both find nothing, both insert, and the second would fail on the unique constraint -- turning a
perfectly ordinary repeat into a 500. Widening the transaction would trade that for a lock held
across an inference call.

``INSERT ... ON CONFLICT DO NOTHING`` has no window. The database evaluates the constraint as
part of the write, one statement, no read-modify-write: a repeat writes nothing and raises
nothing, and there is no interleaving of two callers that produces two rows or an error. That is
what "the database is the final authority for uniqueness" means in practice.

The conflict target is named explicitly rather than inferred from the column list, so a future
second unique constraint on this table cannot silently start absorbing conflicts this method was
never meant to swallow.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.ml.accuracy import ScoredCandidate
from app.ml.accuracy_protocol import VOLATILE_OCCUPANCY_STATUSES
from app.models.booking import BookingRoom, BookingRoomNight
from app.models.prediction import IDENTITY_CONSTRAINT, DemandPrediction


class MlPredictionRepository:
    """Demand predictions: one write, and two bounded reads. One hotel per call, always.

    Stage 6.8 added the write. Stage 6.9 adds the two reads retrospective accuracy needs, and
    no others -- there is still no ``get``, no ``list_for_hotel`` and nothing that answers for
    more than one hotel, because a method that answers for many is one edit away from a method
    that answers for all.

    Neither read commits, rolls back or flushes. The transaction boundary belongs to the
    service, and the Stage 6.9 service is read-only, so nothing on this path writes at all.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        *,
        hotel_id: int,
        target_date: dt.date,
        forecast_horizon_days: int,
        prediction_cutoff: dt.datetime,
        predicted_room_nights: float,
        model_name: str,
        model_version: str,
        feature_version: str,
        dataset_version: str,
        canonical_model_digest: str,
        feature_values: dict[str, Any],
        feature_digest: str,
        request_id: str | None,
    ) -> bool:
        """Record one prediction, or leave the identical one already there untouched.

        Returns whether a row was inserted. ``False`` means an identical prediction -- same
        hotel, target date, horizon, model version and inputs -- was already recorded, which is
        the idempotent case rather than an error. The existing row is not touched: no column is
        rewritten and ``generated_at`` keeps the moment the prediction was first produced.
        """
        statement = (
            insert(DemandPrediction)
            .values(
                hotel_id=hotel_id,
                target_date=target_date,
                forecast_horizon_days=forecast_horizon_days,
                prediction_cutoff=prediction_cutoff,
                predicted_room_nights=predicted_room_nights,
                model_name=model_name,
                model_version=model_version,
                feature_version=feature_version,
                dataset_version=dataset_version,
                canonical_model_digest=canonical_model_digest,
                feature_values=feature_values,
                feature_digest=feature_digest,
                request_id=request_id,
            )
            .on_conflict_do_nothing(constraint=IDENTITY_CONSTRAINT)
        )
        # `rowcount` on a CursorResult is 1 for an insert that happened and 0 for one the
        # conflict target absorbed. Narrowed explicitly because `Session.execute` is typed as
        # returning the generic Result, which has no rowcount.
        result = self._session.execute(statement)
        return bool(getattr(result, "rowcount", 0) == 1)

    # --- reads, for Stage 6.9 -------------------------------------------------------------------

    def scorable_predictions(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date
    ) -> list[ScoredCandidate]:
        """One prediction per ``(target_date, horizon, model_version)``, for one hotel.

        **The selection rule is executed by the database, in this one query.** ``DISTINCT ON``
        over the group, ordered by ``generated_at`` then ``id``, is the protocol's "earliest,
        tie-break lowest id" expressed where it can actually be enforced -- one grouped scan,
        no second round trip, and no window in which application code could pick differently.

        ``id`` earns its place in the ``ORDER BY`` and nowhere else: it makes the ordering
        total, so two rows written in the same microsecond still resolve to one answer, and it
        does not travel out of this method.

        Bounded by ``hotel_id`` and by the two dates, which are the caller's whole vocabulary
        here. There is no parameter through which a second hotel could be named.
        """
        rows = self._session.execute(
            select(
                DemandPrediction.target_date,
                DemandPrediction.forecast_horizon_days,
                DemandPrediction.model_version,
                DemandPrediction.canonical_model_digest,
                DemandPrediction.feature_digest,
                DemandPrediction.feature_values,
                DemandPrediction.predicted_room_nights,
            )
            .where(
                and_(
                    DemandPrediction.hotel_id == hotel_id,
                    DemandPrediction.target_date >= date_from,
                    DemandPrediction.target_date <= date_to,
                )
            )
            .distinct(
                DemandPrediction.target_date,
                DemandPrediction.forecast_horizon_days,
                DemandPrediction.model_version,
            )
            .order_by(
                DemandPrediction.target_date,
                DemandPrediction.forecast_horizon_days,
                DemandPrediction.model_version,
                DemandPrediction.generated_at,
                DemandPrediction.id,
            )
        ).all()

        return [
            ScoredCandidate(
                target_date=row.target_date,
                forecast_horizon_days=row.forecast_horizon_days,
                model_version=row.model_version,
                canonical_model_digest=row.canonical_model_digest,
                feature_digest=row.feature_digest,
                feature_values=dict(row.feature_values),
                # No cast: the column is DOUBLE PRECISION, so the driver already hands back
                # a Python float. Spelling one here would put `float(` in a repository and
                # widen the money-safety exemption to a layer that does not need it.
                predicted_room_nights=row.predicted_room_nights,
            )
            for row in rows
        ]

    def unsettled_allocation_count(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date
    ) -> int:
        """Allocations covering the window whose occupancy membership can still change.

        This is the falsifiability half of the settlement lag. The lag is an operational
        assumption -- see ``app.ml.accuracy_protocol`` -- so the evaluation measures whether it
        held rather than asserting that it did. A non-zero answer means at least one night in
        the scored window may still gain or lose a room night, and the result says so.

        The status set is :data:`~app.ml.accuracy_protocol.VOLATILE_OCCUPANCY_STATUSES`, which
        is *derived* from the existing transition graph and ``OCCUPANCY_STATUSES``. No second
        definition of occupancy semantics is introduced here, and a future migration that
        changed the graph would change this query with it.

        Distinct allocations, not nights: one booking spanning ten nights of the window is one
        thing that might change, not ten.
        """
        return int(
            self._session.execute(
                select(func.count(func.distinct(BookingRoom.id)))
                .select_from(BookingRoomNight)
                .join(BookingRoom, BookingRoom.id == BookingRoomNight.booking_room_id)
                .where(
                    and_(
                        BookingRoomNight.hotel_id == hotel_id,
                        BookingRoomNight.stay_date >= date_from,
                        BookingRoomNight.stay_date <= date_to,
                        BookingRoom.booking_status.in_(VOLATILE_OCCUPANCY_STATUSES),
                    )
                )
            ).scalar_one()
        )


__all__ = ["MlPredictionRepository"]
