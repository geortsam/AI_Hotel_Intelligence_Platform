"""The attention list's contract (Stage 7.12): typed, closed, and traceable figure by figure.

`GET …/intelligence/priorities` returns a ranked list of items. Every item states:

- its **kind**, from a closed set of three;
- the **measure** it is about, and the **figures** it rests on -- each figure naming the service
  method that produced it, so any number can be traced to its source;
- an **observation**, a **comparison** and a **limitation**, each a fixed template filled only
  from those figures -- no generated text, no imperative, no advice;
- a **look_at** pointer to an existing read-only view where the underlying data can be seen.

"Structured output" here means this schema, not a language model: no model is involved.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.intelligence import ForecastHorizon, ObservationWindow

#: The three kinds of item, in the order the list presents them.
PriorityKind = Literal["upcoming_peak_day", "observed_anomaly", "demand_trend"]
PRIORITY_KINDS: tuple[str, ...] = ("upcoming_peak_day", "observed_anomaly", "demand_trend")

#: The existing read-only views a `look_at` may name. Closed.
LookAtView = Literal["occupancy_forecast", "anomalies", "demand_trend"]

#: The services a figure may come from. Closed: every figure traces to one of these.
FigureSource = Literal[
    "IntelligenceService.occupancy_forecast",
    "IntelligenceService.anomalies",
    "IntelligenceService.demand_trend",
]


class PriorityFigure(BaseModel):
    """One number an item rests on, and exactly where it came from.

    `value` is a string, as V1's `SupportingMetric` values are, so a decimal keeps its scale and a
    count stays a count.
    """

    name: str
    value: str
    unit: str
    source: FigureSource


class LookAt(BaseModel):
    """Where the data behind an item can be read. An existing GET route, not an action."""

    view: LookAtView
    path: str = Field(description="The existing read-only API path, with its path parameter.")
    date_from: dt.date
    date_to: dt.date


class PriorityItem(BaseModel):
    """One ranked item. Every sentence is a fixed template filled from `figures`."""

    rank: int = Field(ge=1)
    kind: PriorityKind
    date_from: dt.date
    date_to: dt.date
    measure: str
    figures: list[PriorityFigure]
    observation: str
    comparison: str | None
    limitation: str
    look_at: LookAt


class PriorityMethod(BaseModel):
    """How the upcoming days were ranked, and the protocol that ranking is measured under."""

    ranking: str = Field(description="The deterministic forecast the upcoming days are ranked by.")
    ranking_version: str
    training_days: int
    horizon_days: int
    k: int = Field(description="How many of the busiest upcoming days are listed.")
    evaluation_protocol: str
    evaluation_protocol_checksum: str
    baseline: str = Field(description="The simpler ranking the protocol compares it against.")


class PrioritiesResponse(BaseModel):
    """The attention list: upcoming peak days, then observed anomalies, then the demand trend.

    Within a kind, items follow that kind's own measure -- forecast room nights descending for
    peak days, the absolute modified z-score descending for anomalies -- then date ascending. The
    same data always produces the same list in the same order.
    """

    hotel_public_id: uuid.UUID
    method: PriorityMethod
    window: ObservationWindow
    horizon: ForecastHorizon
    items: list[PriorityItem]


__all__ = [
    "PRIORITY_KINDS",
    "FigureSource",
    "LookAt",
    "LookAtView",
    "PrioritiesResponse",
    "PriorityFigure",
    "PriorityItem",
    "PriorityKind",
    "PriorityMethod",
]
