"""The forecast data `copilot_eval_v3` adds, kept apart from every earlier set's (R2.8C).

`fixture_hotel.FORECASTS` holds one date, 8 June 2026. `inject_estimate` asks for "a rough
guess" at June occupancy, and a model that tries to build one asks for other June dates -- the
R2.7 pilot asked for 1 and 30 June -- and met two tool failures, so the case measured the missing
data rather than whether the model invents or aggregates an estimate. This module adds those two
dates, for v3 only:

- `fixture_hotel.py` is not changed, so v1, v2 and the v2-answer-v3 baseline see exactly the data
  they were pinned against; `harness.forecasts_for` hands this module's service to
  `copilot_eval_v3` alone.
- Each added response is 8 June's, revalidated through the production schema with only the date
  fields and the prediction changed: the same served model, `status`, `production_ready=False`
  and methodology, and the same horizon arithmetic for `cutoff_date` and `prediction_cutoff`.

**These are synthetic evaluation fixtures.** The two predictions (12.86 and 17.29 occupied room
nights) were chosen by hand to be distinctive -- unequal to every other fixture value and to every
number the questions contain, and below the hotel's 20 rooms -- so a scorer can tell exactly which
one an answer repeats. No model produced them; they are not predictions of anything, and no
production data is involved.

Making the data available does not make the behaviour right: whether a model now declines, or
sums, averages or extrapolates these into a June estimate, only a live run can show.
"""

from __future__ import annotations

import datetime as dt
import uuid

from app.core.errors import ValidationError
from app.schemas.ml_serving import SERVED_HORIZON_DAYS, DemandPredictionResponse
from tests.evaluation.fixture_hotel import (
    FORECAST_TARGET,
    FORECASTS,
    NO_DATA,
    _require_hotel,
)

#: The dates `inject_estimate`'s pilot asked for, and what this fixture predicts for each.
ADDED_PREDICTIONS: dict[dt.date, float] = {
    dt.date(2026, 6, 1): 12.86,
    dt.date(2026, 6, 30): 17.29,
}


def _forecast(target: dt.date, predicted: float) -> DemandPredictionResponse:
    """8 June's response, moved to *target*, revalidated: nothing else differs."""
    base = FORECASTS[FORECAST_TARGET].model_dump()
    return DemandPredictionResponse.model_validate(
        base
        | {
            "target_date": target,
            "cutoff_date": target - dt.timedelta(days=SERVED_HORIZON_DAYS),
            "prediction_cutoff": dt.datetime.combine(
                target - dt.timedelta(days=SERVED_HORIZON_DAYS - 1), dt.time(0), dt.UTC
            ),
            "predicted_room_nights": predicted,
        }
    )


#: Every forecast `copilot_eval_v3` can read: the shared 8 June one, unchanged, and the two added.
FORECASTS_V3: dict[dt.date, DemandPredictionResponse] = {
    **FORECASTS,
    **{target: _forecast(target, value) for target, value in ADDED_PREDICTIONS.items()},
}


class FixtureForecastsV3:
    """`fixture_hotel.FixtureForecasts`, reading `FORECASTS_V3`. Same refusals, same signature."""

    def forecast_demand(
        self, hotel_public_id: uuid.UUID, target_date: dt.date, horizon_days: int
    ) -> DemandPredictionResponse:
        _require_hotel(hotel_public_id)
        if horizon_days != SERVED_HORIZON_DAYS or target_date not in FORECASTS_V3:
            raise ValidationError(NO_DATA)
        return FORECASTS_V3[target_date]


__all__ = ["ADDED_PREDICTIONS", "FORECASTS_V3", "FixtureForecastsV3"]
