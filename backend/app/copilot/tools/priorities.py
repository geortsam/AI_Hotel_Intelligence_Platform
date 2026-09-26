"""`get_hotel_priorities` — the attention list, for one bounded observation window (Stage 7.12).

| §7.3 field | Value |
|---|---|
| delegates to | `InsightService.priorities`, unchanged -- the list the route serves |
| min role | viewer — the same as the route |
| input | `date_from`, `date_to`; nothing else is accepted |
| output | `PrioritiesResponse` without `hotel_public_id` |
| side effects | none |
| errors | the service's own `AppError`s (a bad window is a validation error), to the model |

Every item in the output is a fixed template filled from figures an existing service returned,
each figure naming its source. Nothing in it is advice, and the tool takes no action.
"""

from __future__ import annotations

from app.copilot.contracts import (
    HOTEL_IDENTIFIER_FIELD,
    DateRangeArguments,
    ToolContext,
    ToolContract,
    ToolOutput,
)
from app.models.enums import HotelRole
from app.schemas.insight import PriorityItem, PriorityMethod
from app.schemas.intelligence import ForecastHorizon, ObservationWindow


class HotelPrioritiesArguments(DateRangeArguments):
    """The observation window. The upcoming days ranked are the fourteen after it."""


class HotelPrioritiesOutput(ToolOutput):
    method: PriorityMethod
    window: ObservationWindow
    horizon: ForecastHorizon
    items: list[PriorityItem]


CONTRACT = ToolContract(
    name="get_hotel_priorities",
    description=(
        "The current hotel's attention list for an inclusive observation window: the busiest of "
        "the next 14 days by the seasonal forecast, the strongest anomalies observed in the "
        "window, and the booking-demand trend when it moved. Each item states its measure, its "
        "figures and their source, a comparison and a limitation. Descriptive only."
    ),
    min_role=HotelRole.VIEWER,
    input_model=HotelPrioritiesArguments,
    output_model=HotelPrioritiesOutput,
    delegates_to="InsightService.priorities",
)


def run(context: ToolContext, arguments: HotelPrioritiesArguments) -> HotelPrioritiesOutput:
    response = context.services.insight.priorities(
        context.hotel_public_id, arguments.date_from, arguments.date_to
    )
    return HotelPrioritiesOutput.model_validate(
        response.model_dump(exclude={HOTEL_IDENTIFIER_FIELD})
    )
