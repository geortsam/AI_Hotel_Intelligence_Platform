"""`get_demand_forecast` — §7.2 row 4. The trained model's prediction for one date.

- **delegates to** — `DemandPredictionService.forecast_demand`, unchanged.
- **min role** — viewer, the same as `GET …/ml/demand-forecast`.
- **input** — `target_date` only. The horizon is **not** an argument: the served model fixes it.
- **output** — `DemandPredictionResponse` without `hotel_public_id`: the prediction, its full
  provenance, and the model metadata including its `production_ready` flag and methodology.
- **side effects** — **`records_served_prediction`**; see below.
- **errors** — the service's own `AppError`s (model unavailable, insufficient history, …),
  returned to the model.

## The one declared side effect

`forecast_demand` records every prediction it serves (Stage 6.8), so that it can later be scored
by the accuracy protocol against what actually happened. §4.3 and §7.3 say "read-only", and this
tool is not strictly that. Stage 7.6 decided to **allow and declare** it rather than bypass the
service: the write is the service's own, it is idempotent under
`uq_demand_predictions_identity`, it changes no business record, and a forecast shown to a model
is a forecast served — not recording it would make the accuracy of what the copilot said
unmeasurable. Recorded in Amendment A1.

## Why the horizon is fixed here

The served model predicts exactly `SERVED_HORIZON_DAYS` ahead. Letting a model choose a horizon
would only let it ask for one the service refuses; passing the constant is the same thing the
route does by default and gives the model nothing to get wrong.
"""

from __future__ import annotations

import datetime as dt

from pydantic import Field

from app.copilot.contracts import (
    HOTEL_IDENTIFIER_FIELD,
    ToolArguments,
    ToolContext,
    ToolContract,
    ToolOutput,
)
from app.models.enums import HotelRole
from app.schemas.ml_serving import SERVED_HORIZON_DAYS, DemandModelMetadata


class DemandForecastArguments(ToolArguments):
    target_date: dt.date = Field(
        description=(
            "The date to forecast occupied room nights for (YYYY-MM-DD). The model forecasts "
            f"{SERVED_HORIZON_DAYS} days ahead of the last day of history it may use."
        )
    )


class DemandForecastOutput(ToolOutput):
    target_date: dt.date
    forecast_horizon_days: int
    cutoff_date: dt.date
    prediction_cutoff: dt.datetime
    predicted_room_nights: float
    model: DemandModelMetadata
    features_used: list[str]


CONTRACT = ToolContract(
    name="get_demand_forecast",
    description=(
        "The demand model's forecast of occupied room nights for the current hotel on one "
        "target date, with the model's version, status, production-readiness flag, "
        "methodology and the features it used. It is a model estimate, not a measured figure."
    ),
    min_role=HotelRole.VIEWER,
    input_model=DemandForecastArguments,
    output_model=DemandForecastOutput,
    delegates_to="DemandPredictionService.forecast_demand",
    side_effect="records_served_prediction",
)


def run(context: ToolContext, arguments: DemandForecastArguments) -> DemandForecastOutput:
    response = context.services.demand_prediction.forecast_demand(
        context.hotel_public_id, arguments.target_date, SERVED_HORIZON_DAYS
    )
    return DemandForecastOutput.model_validate(
        response.model_dump(exclude={HOTEL_IDENTIFIER_FIELD})
    )
