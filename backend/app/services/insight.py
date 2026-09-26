"""The attention list: a ranked, deterministic reading of one hotel's own data (Stage 7.12).

`GET …/intelligence/priorities`. Architecture §3's `insight.py`; `docs/attention-list.md` is
the design document. Read-only, computed on request, stored nowhere, and no language model is
involved anywhere: every sentence is a fixed template filled from figures an existing service
returned.

## What it composes, and only that

    upcoming_peak_day   IntelligenceService.occupancy_forecast -- the seasonal day-of-week median
                        over the TRAINING_DAYS days ending at the window's last day, for the
                        HORIZON_DAYS days after it; the K busiest by forecast room nights
    observed_anomaly    IntelligenceService.anomalies over the observation window; the
                        MAX_ANOMALY_ITEMS with the largest absolute modified z-score
    demand_trend        IntelligenceService.demand_trend over the observation window, when it
                        classifies booking demand as increasing or decreasing

TRAINING_DAYS, HORIZON_DAYS and K come from `insight_ranking_v1`, the protocol the day ranking is
measured under offline -- so what is served is what was measured. No other service, no new
metric, no second forecast, and not the learned demand model.

## Order

Kinds in the order above; within a kind, by its own measure (forecast room nights, absolute
z-score) descending, then date ascending, then metric name. Total and deterministic: the same data
always produces the same list.

## What an item may say

An observation, the measure, the figures, a comparison and a limitation -- each a template below
-- and a pointer to the existing view where the data can be read. It never says what to do: no
pricing, staffing, allocation or any other operational instruction, and nothing automated. A test
reads every template and refuses imperative vocabulary.

## Whose data

Every call resolves the path hotel through the scope resolver before anything is read, and each
composed service resolves it again. A hotel the caller is not a member of is the usual 404.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from app.ml.ranking_protocol import BASELINE_METHOD, HORIZON_DAYS, PROTOCOL, TRAINING_DAYS, K
from app.ml.timeseries import MODEL_NAME, MODEL_VERSION
from app.schemas.insight import (
    LookAt,
    LookAtView,
    PrioritiesResponse,
    PriorityFigure,
    PriorityItem,
    PriorityMethod,
)
from app.schemas.intelligence import (
    AnomalyPoint,
    DemandTrendResponse,
    ForecastHorizon,
    OccupancyForecastPoint,
)
from app.services.intelligence import IntelligenceService
from app.services.scope import HotelScopeResolver

#: How many observed anomalies the list carries at most.
MAX_ANOMALY_ITEMS = 3

FORECAST_SOURCE = "IntelligenceService.occupancy_forecast"
ANOMALY_SOURCE = "IntelligenceService.anomalies"
TREND_SOURCE = "IntelligenceService.demand_trend"

#: The existing read-only views an item may point to.
VIEW_PATHS: dict[LookAtView, str] = {
    "occupancy_forecast": "/api/v1/hotels/{hotel_public_id}/intelligence/forecast/occupancy",
    "anomalies": "/api/v1/hotels/{hotel_public_id}/intelligence/anomalies",
    "demand_trend": "/api/v1/hotels/{hotel_public_id}/intelligence/demand-trend",
}

# --- the templates: closed, descriptive, filled only from the item's own figures -----------------

PEAK_OBSERVATION = (
    "{date} ranks {rank} of {days} upcoming days by forecast occupied room nights "
    "({forecast} forecast)."
)
PEAK_COMPARISON = (
    "{on_the_books} room nights are already on the books for that day, of {available} available."
)
PEAK_LIMITATION = (
    "A seasonal day-of-week median over the {training} days before the horizon: an estimate, not "
    "a booking. How this ranking of days compares with a simpler one is measured offline under "
    "{protocol}; no operational benefit is claimed."
)
ANOMALY_OBSERVATION = (
    "On {date}, {metric} was {value}, {direction} the window median of {median} (modified "
    "z-score {z})."
)
ANOMALY_COMPARISON = "A day is flagged when the absolute modified z-score exceeds {threshold}."
ANOMALY_LIMITATION = (
    "A statistical flag over the observation window; it records that the day differed, not why."
)
TREND_OBSERVATION = (
    "Bookings taken per day moved from a median of {earlier} in the first half of the window to "
    "{recent} in the second half, which the trend method classifies as {direction}."
)
TREND_COMPARISON = (
    "The relative change was {change}, against a classification threshold of {threshold}."
)
TREND_LIMITATION = (
    "A comparison of two half-window medians of bookings taken; it describes the window, not "
    "what comes next."
)

TEMPLATES: tuple[str, ...] = (
    PEAK_OBSERVATION,
    PEAK_COMPARISON,
    PEAK_LIMITATION,
    ANOMALY_OBSERVATION,
    ANOMALY_COMPARISON,
    ANOMALY_LIMITATION,
    TREND_OBSERVATION,
    TREND_COMPARISON,
    TREND_LIMITATION,
)


def _text(value: Decimal | int | None) -> str:
    return "n/a" if value is None else str(value)


class InsightService:
    """Assembles the attention list from existing intelligence outputs. Writes nothing."""

    def __init__(self, intelligence: IntelligenceService, scope: HotelScopeResolver) -> None:
        self._intelligence = intelligence
        self._scope = scope

    def priorities(
        self, hotel_public_id: uuid.UUID, date_from: dt.date, date_to: dt.date
    ) -> PrioritiesResponse:
        """The attention list for the observation window [date_from, date_to] and the
        HORIZON_DAYS days after it. See the module docstring."""
        hotel = self._scope.require_hotel(hotel_public_id)
        anomalies = self._intelligence.anomalies(hotel_public_id, date_from, date_to)
        trend = self._intelligence.demand_trend(hotel_public_id, date_from, date_to)
        horizon_from = date_to + dt.timedelta(days=1)
        horizon_to = date_to + dt.timedelta(days=HORIZON_DAYS)
        forecast = self._intelligence.occupancy_forecast(
            hotel_public_id, horizon_from, horizon_to, TRAINING_DAYS
        )

        items: list[PriorityItem] = []
        items += self._peaks(forecast.points, forecast.horizon)
        items += self._anomalies(anomalies.anomalies, date_from, date_to)
        items += self._trend(trend)
        ranked = [
            item.model_copy(update={"rank": position}) for position, item in enumerate(items, 1)
        ]

        return PrioritiesResponse(
            hotel_public_id=hotel.public_id,
            method=PriorityMethod(
                ranking=MODEL_NAME,
                ranking_version=MODEL_VERSION,
                training_days=TRAINING_DAYS,
                horizon_days=HORIZON_DAYS,
                k=K,
                evaluation_protocol=PROTOCOL.identity,
                evaluation_protocol_checksum=PROTOCOL.checksum,
                baseline=BASELINE_METHOD,
            ),
            window=anomalies.window,
            horizon=ForecastHorizon(date_from=horizon_from, date_to=horizon_to, days=HORIZON_DAYS),
            items=ranked,
        )

    # --- the three kinds -------------------------------------------------------------------------

    @staticmethod
    def _peaks(
        points: list[OccupancyForecastPoint], horizon: ForecastHorizon
    ) -> list[PriorityItem]:
        scored = [point for point in points if point.predicted_room_nights is not None]
        busiest = sorted(scored, key=lambda p: (-(p.predicted_room_nights or Decimal(0)), p.date))
        items: list[PriorityItem] = []
        for position, point in enumerate(busiest[:K], 1):
            figures = [
                PriorityFigure(
                    name="predicted_room_nights",
                    value=_text(point.predicted_room_nights),
                    unit="room_nights",
                    source=FORECAST_SOURCE,
                ),
                PriorityFigure(
                    name="on_the_books_room_nights",
                    value=_text(point.on_the_books_room_nights),
                    unit="room_nights",
                    source=FORECAST_SOURCE,
                ),
                PriorityFigure(
                    name="available_room_nights",
                    value=_text(point.available_room_nights),
                    unit="room_nights",
                    source=FORECAST_SOURCE,
                ),
                PriorityFigure(
                    name="interval_lower",
                    value=_text(point.interval_lower),
                    unit="room_nights",
                    source=FORECAST_SOURCE,
                ),
                PriorityFigure(
                    name="interval_upper",
                    value=_text(point.interval_upper),
                    unit="room_nights",
                    source=FORECAST_SOURCE,
                ),
            ]
            items.append(
                PriorityItem(
                    rank=1,
                    kind="upcoming_peak_day",
                    date_from=point.date,
                    date_to=point.date,
                    measure="predicted_room_nights",
                    figures=figures,
                    observation=PEAK_OBSERVATION.format(
                        date=point.date.isoformat(),
                        rank=position,
                        days=horizon.days,
                        forecast=_text(point.predicted_room_nights),
                    ),
                    comparison=PEAK_COMPARISON.format(
                        on_the_books=point.on_the_books_room_nights,
                        available=point.available_room_nights,
                    ),
                    limitation=PEAK_LIMITATION.format(
                        training=TRAINING_DAYS, protocol=PROTOCOL.identity
                    ),
                    look_at=LookAt(
                        view="occupancy_forecast",
                        path=VIEW_PATHS["occupancy_forecast"],
                        date_from=horizon.date_from,
                        date_to=horizon.date_to,
                    ),
                )
            )
        return items

    @staticmethod
    def _anomalies(
        points: list[AnomalyPoint], date_from: dt.date, date_to: dt.date
    ) -> list[PriorityItem]:
        strongest = sorted(points, key=lambda p: (-abs(p.modified_z_score), p.date, p.metric))[
            :MAX_ANOMALY_ITEMS
        ]
        items: list[PriorityItem] = []
        for point in strongest:
            figures = [
                PriorityFigure(
                    name="value", value=_text(point.value), unit=point.metric, source=ANOMALY_SOURCE
                ),
                PriorityFigure(
                    name="median",
                    value=_text(point.median),
                    unit=point.metric,
                    source=ANOMALY_SOURCE,
                ),
                PriorityFigure(
                    name="median_absolute_deviation",
                    value=_text(point.median_absolute_deviation),
                    unit=point.metric,
                    source=ANOMALY_SOURCE,
                ),
                PriorityFigure(
                    name="modified_z_score",
                    value=_text(point.modified_z_score),
                    unit="z_score",
                    source=ANOMALY_SOURCE,
                ),
                PriorityFigure(
                    name="threshold",
                    value=_text(point.threshold),
                    unit="z_score",
                    source=ANOMALY_SOURCE,
                ),
            ]
            items.append(
                PriorityItem(
                    rank=1,
                    kind="observed_anomaly",
                    date_from=point.date,
                    date_to=point.date,
                    measure=point.metric,
                    figures=figures,
                    observation=ANOMALY_OBSERVATION.format(
                        date=point.date.isoformat(),
                        metric=point.metric,
                        value=_text(point.value),
                        direction=point.direction,
                        median=_text(point.median),
                        z=_text(point.modified_z_score),
                    ),
                    comparison=ANOMALY_COMPARISON.format(threshold=_text(point.threshold)),
                    limitation=ANOMALY_LIMITATION,
                    look_at=LookAt(
                        view="anomalies",
                        path=VIEW_PATHS["anomalies"],
                        date_from=date_from,
                        date_to=date_to,
                    ),
                )
            )
        return items

    @staticmethod
    def _trend(trend: DemandTrendResponse) -> list[PriorityItem]:
        if trend.direction not in ("increasing", "decreasing"):
            return []
        figures = [
            PriorityFigure(
                name="earlier_median",
                value=_text(trend.earlier_median),
                unit=trend.metric,
                source=TREND_SOURCE,
            ),
            PriorityFigure(
                name="recent_median",
                value=_text(trend.recent_median),
                unit=trend.metric,
                source=TREND_SOURCE,
            ),
            PriorityFigure(
                name="relative_change",
                value=_text(trend.relative_change),
                unit="ratio",
                source=TREND_SOURCE,
            ),
            PriorityFigure(
                name="threshold", value=_text(trend.threshold), unit="ratio", source=TREND_SOURCE
            ),
        ]
        return [
            PriorityItem(
                rank=1,
                kind="demand_trend",
                date_from=trend.window.date_from,
                date_to=trend.window.date_to,
                measure=trend.metric,
                figures=figures,
                observation=TREND_OBSERVATION.format(
                    earlier=_text(trend.earlier_median),
                    recent=_text(trend.recent_median),
                    direction=trend.direction,
                ),
                comparison=TREND_COMPARISON.format(
                    change=_text(trend.relative_change), threshold=_text(trend.threshold)
                ),
                limitation=TREND_LIMITATION,
                look_at=LookAt(
                    view="demand_trend",
                    path=VIEW_PATHS["demand_trend"],
                    date_from=trend.window.date_from,
                    date_to=trend.window.date_to,
                ),
            )
        ]


__all__ = [
    "MAX_ANOMALY_ITEMS",
    "TEMPLATES",
    "VIEW_PATHS",
    "InsightService",
]
