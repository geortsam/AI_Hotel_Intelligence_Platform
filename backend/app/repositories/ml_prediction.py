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

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.prediction import IDENTITY_CONSTRAINT, DemandPrediction


class MlPredictionRepository:
    """Writes demand predictions. One hotel per call, and no method that reads across hotels.

    There is deliberately no ``list_for_hotel`` and no ``get`` here. Stage 6.8 adds no read
    path, and a repository method that exists before anything calls it is a method whose tenant
    scoping nobody has had to think about yet.
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


__all__ = ["MlPredictionRepository"]
