"""What the API and the copilot say arrivals and departures are (Issue F4).

``tests/integration/test_stay_flow_statuses.py`` proves which bookings are counted. This file
proves the counting rule is stated where a reader meets the figures: the OpenAPI descriptions of
both payloads, and the two copilot tools that return them -- so a model quoting "arrivals" is told
that cancelled, no-show and pending bookings are not in them.
"""

from __future__ import annotations

import pytest

from app.copilot.tools.daily_series import CONTRACT as DAILY_SERIES
from app.copilot.tools.hotel_kpis import CONTRACT as HOTEL_KPIS
from app.schemas.analytics import DailyMetricsRow, StayFlowMetrics

RULE = "cancelled, no-show and pending bookings are not counted"


@pytest.mark.parametrize(
    ("schema", "field"),
    [
        (StayFlowMetrics, "arrivals"),
        (StayFlowMetrics, "departures"),
        (DailyMetricsRow, "arrivals"),
        (DailyMetricsRow, "departures"),
    ],
)
def test_the_api_describes_who_is_counted(schema: type, field: str) -> None:
    description = schema.model_fields[field].description  # type: ignore[attr-defined]
    assert description is not None
    assert RULE in description.lower(), description


@pytest.mark.parametrize("contract", [HOTEL_KPIS, DAILY_SERIES], ids=lambda c: c.name)
def test_each_copilot_tool_tells_the_model_who_is_counted(contract: object) -> None:
    description = contract.description  # type: ignore[attr-defined]
    assert "confirmed, checked-in and checked-out bookings only" in description
