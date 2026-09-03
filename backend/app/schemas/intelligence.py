"""Intelligence API contracts: forecasts, trend, anomalies and structured insights.

**Predictions are never mixed with facts.** A hotel already knows part of its future: bookings
for next month exist in the database today. An occupancy forecast that ignored those would be
worse than useless, and one that silently merged them with a statistical estimate would let
an operator mistake a guess for a confirmed reservation. Every forecast day therefore carries
``on_the_books_room_nights`` -- an actual count from ``booking_room_nights`` -- **beside** the
predicted figure, never blended into it.

**Every response carries its provenance.** Model name, version, methodology, training window,
horizon and per-point method are all present, so a number in a chart can be re-derived from
the response alone.

**Currency follows the Stage 3B.10 architecture**: per-currency buckets, independent forecasts
per currency, no FX anywhere, no cross-currency total.

**No free-form text is generated.** Insight explanations are deterministic templates filled
from the same numbers the response already carries. There is no LLM in this stage.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: Bounds on the history a request may ask the models to learn from. Explicit rather than
#: open-ended so one request cannot scan an unbounded span.
MIN_TRAINING_DAYS = 14
MAX_TRAINING_DAYS = 365
DEFAULT_TRAINING_DAYS = 90

#: Bounds on how far ahead a forecast may be asked to run. A seasonal-naive baseline degrades
#: quickly; ninety days is already generous for it and the limitation is documented.
MAX_HORIZON_DAYS = 90
DEFAULT_HORIZON_DAYS = 7

#: Bounds on an observation window for trend and anomaly work.
MAX_OBSERVATION_DAYS = 366

ForecastMethodLiteral = Literal["seasonal_dow_median", "overall_median", "insufficient_data"]
TrendDirectionLiteral = Literal["increasing", "decreasing", "stable", "insufficient_data"]
SeverityLiteral = Literal["info", "warning", "critical"]


class ModelMetadata(BaseModel):
    """Everything needed to reproduce a prediction, minus the data itself.

    ``generated_at`` is provenance, not an input: it records when the response was built and
    is the only field that changes between two otherwise identical requests. Every predicted
    number is a pure function of the hotel, the dates and the model version.
    """

    model_config = ConfigDict(protected_namespaces=())

    model_name: str
    model_version: str
    #: A one-line description of the arithmetic, so the response explains itself.
    methodology: str
    generated_at: dt.datetime


class TrainingWindow(BaseModel):
    """The history a forecast was fitted on.

    It always ends strictly **before** the first forecast day. That boundary is the leakage
    guard: no observation from inside the horizon can reach the model.
    """

    date_from: dt.date
    date_to: dt.date
    days: int
    #: Observations actually available. Equal to ``days`` -- the series is densified, because
    #: a day with no bookings is a real zero rather than a missing reading.
    observations: int


class ForecastHorizon(BaseModel):
    """The future span a forecast covers. Both bounds inclusive."""

    date_from: dt.date
    date_to: dt.date
    days: int


class OccupancyForecastPoint(BaseModel):
    """One forecast day: what is already booked, and what the model expects."""

    date: dt.date

    #: **Actual, not predicted.** Room nights already on the books for this date, counted the
    #: same way Stage 3B.10 counts occupancy. A fact the hotel can rely on.
    on_the_books_room_nights: int
    #: Capacity, on the same current-inventory basis Stage 3B.10 documents.
    available_room_nights: int

    #: **Predicted.** Null when the training window holds no usable history; no number is
    #: invented to fill the gap.
    predicted_room_nights: decimal.Decimal | None
    predicted_occupancy_rate: decimal.Decimal | None
    #: Robust prediction interval at ``confidence_level``. Null alongside the prediction.
    interval_lower: decimal.Decimal | None
    interval_upper: decimal.Decimal | None
    confidence_level: decimal.Decimal | None

    #: Which model produced THIS point. A single response can mix methods.
    method: ForecastMethodLiteral
    #: Training observations this point's estimate rests on.
    observations: int
    #: True when the prediction was reduced to available capacity. The schema's own
    #: ``ck_daily_hotel_metrics_occupied_rooms_within_available`` asserts occupied <=
    #: available, so a prediction above capacity is corrected rather than published.
    capacity_clamped: bool


class OccupancyForecastResponse(BaseModel):
    hotel_public_id: uuid.UUID
    model: ModelMetadata
    training_window: TrainingWindow
    horizon: ForecastHorizon
    points: list[OccupancyForecastPoint]


class RevenueForecastPoint(BaseModel):
    """One forecast day within one currency."""

    date: dt.date
    #: **Actual.** Room revenue already on the books for this date, in this currency, from
    #: booking_room_nights.rate -- never from payments and never from a list price.
    on_the_books_room_revenue: decimal.Decimal
    #: **Predicted.** Null when history is insufficient.
    predicted_room_revenue: decimal.Decimal | None
    interval_lower: decimal.Decimal | None
    interval_upper: decimal.Decimal | None
    confidence_level: decimal.Decimal | None
    method: ForecastMethodLiteral
    observations: int


class CurrencyForecast(BaseModel):
    """An independent forecast for one currency.

    Currencies are forecast separately and never combined. There is no FX rate in this
    codebase and the hotel's own currency is not a conversion target.
    """

    currency: str
    points: list[RevenueForecastPoint]


class RevenueForecastResponse(BaseModel):
    hotel_public_id: uuid.UUID
    model: ModelMetadata
    training_window: TrainingWindow
    horizon: ForecastHorizon
    #: One entry per currency with history. A hotel with none gets an empty list, not a zero.
    currencies: list[CurrencyForecast]
    is_multi_currency: bool


class ObservationWindow(BaseModel):
    """The past span a trend or anomaly scan looked at. Both bounds inclusive."""

    date_from: dt.date
    date_to: dt.date
    days: int
    observations: int


class DemandTrendResponse(BaseModel):
    """Booking-demand direction, with the two medians that produced it.

    The window is split in half and the halves' medians compared; the classification is
    ``increasing`` or ``decreasing`` when the relative change exceeds ``threshold``, and
    ``stable`` otherwise. Both medians and the threshold are returned so the answer can be
    recomputed by hand.
    """

    hotel_public_id: uuid.UUID
    model: ModelMetadata
    window: ObservationWindow

    #: Counted by bookings.booked_at -- demand as it was TAKEN, which is what a demand trend
    #: is about. Stay-dated counts answer a different question.
    metric: str
    direction: TrendDirectionLiteral
    earlier_median: decimal.Decimal | None
    recent_median: decimal.Decimal | None
    relative_change: decimal.Decimal | None
    threshold: decimal.Decimal


class AnomalyPoint(BaseModel):
    """One flagged day, carrying the whole statistical basis for the flag."""

    metric: str
    date: dt.date
    value: decimal.Decimal
    #: The window median the value was judged against.
    median: decimal.Decimal
    #: The window's median absolute deviation -- the robust spread.
    median_absolute_deviation: decimal.Decimal
    #: 0.6745 * (value - median) / MAD.
    modified_z_score: decimal.Decimal
    threshold: decimal.Decimal
    direction: Literal["above", "below"]


class AnomalyResponse(BaseModel):
    """Anomalies across the observed metrics, ordered by metric then date."""

    hotel_public_id: uuid.UUID
    model: ModelMetadata
    window: ObservationWindow
    #: The metrics that were scanned, named so an empty result is unambiguous: nothing was
    #: unusual, rather than nothing was looked at.
    metrics_scanned: list[str]
    anomalies: list[AnomalyPoint]


class SupportingMetric(BaseModel):
    """One number an insight rests on. Values are strings so a Decimal keeps its scale and a
    count stays a count."""

    name: str
    value: str
    #: Present only where the metric is monetary; absent elsewhere rather than defaulted.
    currency: str | None = None


class Insight(BaseModel):
    """One structured, deterministic finding.

    The explanation is a template filled from the numbers in ``supporting_metrics``. It is
    not generated text, and the same data always produces the same sentence.
    """

    type: str
    severity: SeverityLiteral
    title: str
    explanation: str
    supporting_metrics: list[SupportingMetric]
    date_from: dt.date
    date_to: dt.date
    #: Present where the finding rests on a model with a stated confidence; null for a
    #: statement of fact.
    confidence: decimal.Decimal | None = None


class InsightsResponse(BaseModel):
    """Insights assembled from the trend, anomaly and forecast outputs.

    Ordered by severity then type then date, so the same data always yields the same list in
    the same order.
    """

    hotel_public_id: uuid.UUID
    model: ModelMetadata
    window: ObservationWindow
    horizon: ForecastHorizon
    insights: list[Insight]


#: Shared query-parameter descriptions, so the router and the docs cannot drift apart.
TRAINING_DAYS_FIELD = Field(
    default=DEFAULT_TRAINING_DAYS,
    ge=MIN_TRAINING_DAYS,
    le=MAX_TRAINING_DAYS,
    description=(
        "How many days of history immediately BEFORE date_from the model may learn from. "
        "A fixed count, so the same request always selects the same window."
    ),
)


__all__ = [
    "DEFAULT_HORIZON_DAYS",
    "DEFAULT_TRAINING_DAYS",
    "MAX_HORIZON_DAYS",
    "MAX_OBSERVATION_DAYS",
    "MAX_TRAINING_DAYS",
    "MIN_TRAINING_DAYS",
    "TRAINING_DAYS_FIELD",
    "AnomalyPoint",
    "AnomalyResponse",
    "CurrencyForecast",
    "DemandTrendResponse",
    "ForecastHorizon",
    "ForecastMethodLiteral",
    "Insight",
    "InsightsResponse",
    "ModelMetadata",
    "ObservationWindow",
    "OccupancyForecastPoint",
    "OccupancyForecastResponse",
    "RevenueForecastPoint",
    "RevenueForecastResponse",
    "SeverityLiteral",
    "SupportingMetric",
    "TrainingWindow",
    "TrendDirectionLiteral",
]
