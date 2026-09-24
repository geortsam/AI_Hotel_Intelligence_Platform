"""`get_forecast_accuracy` — §7.2 row 5. Measured error over this hotel's settled predictions.

- **delegates to** — `ForecastPerformanceService.forecast_accuracy`, unchanged.
- **min role** — **manager**, the same as `GET …/ml/forecast-accuracy`.
- **input** — `as_of_date`, `window_from`, `window_to`; the service bounds the window.
- **output** — `ForecastAccuracyResponse` without `hotel_public_id`, including the measurement
  block whose `establishes_production_accuracy` is false and whose statement says so.
- **side effects** — none.
- **errors** — the service's own `AppError`s, returned to the model.

§7.2 named `DemandAccuracyService.evaluate`. This tool delegates one layer up, to the Stage 7.3
`ForecastPerformanceService`, deliberately: `evaluate` returns the raw evaluation, which carries
the model and protocol digests §7.4 forbids a tool to return and applies no window bound. The
Stage 7.3 service is exactly the projection that makes that evaluation publishable, and it is
the one the route uses — so the model sees what a manager's browser sees, and nothing the route
withholds. Corrected in Amendment A1.
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
from app.schemas.ml_performance import MeasurementMetadata, ModelVersionAccuracyResponse
from app.services.ml_performance import MAX_WINDOW_DAYS


class ForecastAccuracyArguments(ToolArguments):
    as_of_date: dt.date = Field(description="The day the measurement is made as of (YYYY-MM-DD).")
    window_from: dt.date = Field(description="Earliest prediction target date, inclusive.")
    window_to: dt.date = Field(
        description=(
            f"Latest prediction target date, inclusive. At most {MAX_WINDOW_DAYS} days after "
            "window_from."
        )
    )


class ForecastAccuracyOutput(ToolOutput):
    as_of_date: dt.date
    window_from: dt.date
    window_to: dt.date
    scored_from: dt.date | None
    scored_to: dt.date | None
    settlement_lag_days: int
    candidates: int
    ineligible_by_settlement: int
    out_of_scope_model_digest: int
    unsettled_allocations: int
    settled: bool
    by_model_version: list[ModelVersionAccuracyResponse]
    measurement: MeasurementMetadata


CONTRACT = ToolContract(
    name="get_forecast_accuracy",
    description=(
        "Measured error (MAE, RMSE, sMAPE) of the demand model's past predictions for the "
        "current hotel, over settled predictions whose target date falls in a window, split by "
        "model version and calibration segment. Measures past predictions only; it does not "
        "establish production accuracy."
    ),
    min_role=HotelRole.MANAGER,
    input_model=ForecastAccuracyArguments,
    output_model=ForecastAccuracyOutput,
    delegates_to="ForecastPerformanceService.forecast_accuracy",
)


def run(context: ToolContext, arguments: ForecastAccuracyArguments) -> ForecastAccuracyOutput:
    response = context.services.forecast_performance.forecast_accuracy(
        context.hotel_public_id,
        as_of_date=arguments.as_of_date,
        window_from=arguments.window_from,
        window_to=arguments.window_to,
    )
    return ForecastAccuracyOutput.model_validate(
        response.model_dump(exclude={HOTEL_IDENTIFIER_FIELD})
    )
