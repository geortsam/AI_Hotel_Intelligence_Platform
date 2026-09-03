"""Robust baselines for short, weekly-seasonal hotel series.

Three models, each chosen because it is the simplest thing that is statistically defensible
on this data and because an operator can be told exactly why it said what it said:

* **Seasonal-naive day-of-week median** for forecasting. Hotel occupancy and room revenue are
  dominated by day-of-week effects -- a Saturday resembles other Saturdays far more than it
  resembles the preceding Friday. The median rather than the mean because one conference or
  one closure would otherwise drag a short window badly.
* **Modified z-score on the median absolute deviation** for anomalies (Iglewicz & Hoaglin,
  threshold 3.5). Mean-and-standard-deviation flagging is self-defeating on this data: the
  outlier inflates the very standard deviation used to judge it, so real spikes hide.
* **Split-window median comparison** for trend, with an explicit relative threshold. Both
  halves and the threshold are returned, so the classification can be recomputed by hand.

Everything is computed in :class:`~decimal.Decimal`. Money is exact in this codebase and must
stay exact, and Decimal arithmetic is bit-for-bit reproducible where float summation order is
not -- which matters because reproducibility is an explicit requirement of this stage.

**No training data is ever fabricated.** A gap in the source data is a real zero only where
the domain says so (a day with no bookings genuinely had no bookings); a window with no
history at all yields ``insufficient_data`` and no number.
"""

from __future__ import annotations

import datetime as dt
import decimal
import statistics
from enum import StrEnum
from typing import NamedTuple

#: Identifies the forecasting model in every response, so a stored prediction can be traced
#: to the code that produced it. Bump the version when the arithmetic changes.
MODEL_NAME = "seasonal-naive-dow-median"
MODEL_VERSION = "1.0.0"

#: Scales a median absolute deviation to a standard-deviation equivalent for a normal
#: distribution. The standard constant, not a tuned parameter.
MAD_TO_SIGMA = decimal.Decimal("1.4826")
#: Two-sided 95% normal quantile, used for the prediction interval.
CONFIDENCE_Z = decimal.Decimal("1.96")
CONFIDENCE_LEVEL = decimal.Decimal("0.95")

#: Iglewicz & Hoaglin's constant and recommended cut-off for the modified z-score.
MODIFIED_Z_CONSTANT = decimal.Decimal("0.6745")
ANOMALY_THRESHOLD = decimal.Decimal("3.5")

#: A day-of-week bucket needs at least this many observations before its own median is
#: preferred to the whole window's. With one observation a "seasonal" median is just that
#: single value wearing a statistical hat.
MIN_BUCKET_OBSERVATIONS = 2
#: Below this many observations in total, nothing is forecast and nothing is flagged.
MIN_TRAINING_OBSERVATIONS = 7

#: Relative change between the two window halves before demand is called moving rather than
#: stable. Deliberately explicit and returned in the response.
TREND_THRESHOLD = decimal.Decimal("0.10")

ZERO = decimal.Decimal("0")


class ForecastMethod(StrEnum):
    """Which model actually produced a point -- reported per point, not per request.

    A single response can mix these: a Tuesday with four prior Tuesdays gets the seasonal
    median while a Sunday with one gets the window median. Hiding that behind one header
    would misrepresent the weaker points.
    """

    SEASONAL_DOW_MEDIAN = "seasonal_dow_median"
    OVERALL_MEDIAN = "overall_median"
    INSUFFICIENT_DATA = "insufficient_data"


class Observation(NamedTuple):
    """One dated value from the training window."""

    date: dt.date
    value: decimal.Decimal


class ForecastPoint(NamedTuple):
    """One predicted day, with the evidence behind it."""

    date: dt.date
    #: None when the method is INSUFFICIENT_DATA: no number is invented.
    value: decimal.Decimal | None
    lower: decimal.Decimal | None
    upper: decimal.Decimal | None
    method: ForecastMethod
    #: How many training observations this point's estimate rests on.
    observations: int


class Anomaly(NamedTuple):
    """One flagged day, with everything needed to re-derive the judgement."""

    date: dt.date
    value: decimal.Decimal
    median: decimal.Decimal
    deviation: decimal.Decimal
    score: decimal.Decimal
    #: "above" or "below" the median.
    direction: str


class TrendResult(NamedTuple):
    """A demand-direction classification and the two numbers that produced it."""

    #: "increasing", "decreasing", "stable" or "insufficient_data".
    direction: str
    earlier_median: decimal.Decimal | None
    recent_median: decimal.Decimal | None
    #: (recent - earlier) / |earlier|, or None when earlier is zero or data is short.
    relative_change: decimal.Decimal | None
    observations: int


def _median(values: list[decimal.Decimal]) -> decimal.Decimal:
    return decimal.Decimal(statistics.median(values))


def _mad(values: list[decimal.Decimal], centre: decimal.Decimal) -> decimal.Decimal:
    """Median absolute deviation about *centre*.

    Zero for a constant series, which is meaningful rather than a failure: a series that has
    never varied offers no basis for calling any value unusual.
    """
    return _median([abs(value - centre) for value in values])


def forecast_series(
    observations: list[Observation],
    horizon: list[dt.date],
    *,
    minimum_observations: int = MIN_TRAINING_OBSERVATIONS,
) -> list[ForecastPoint]:
    """Forecast each date in *horizon* from *observations*, by day-of-week median.

    **The caller is responsible for the temporal boundary**, and the service that calls this
    builds ``observations`` strictly before the horizon. This function cannot leak on its own
    -- it never looks at a horizon date's value because it is not given one -- but it also
    does not police the caller, so the guard is asserted in the service's tests.

    ``horizon`` order is preserved exactly; the result is one point per requested date.
    """
    if len(observations) < minimum_observations:
        return [
            ForecastPoint(
                date=day,
                value=None,
                lower=None,
                upper=None,
                method=ForecastMethod.INSUFFICIENT_DATA,
                observations=len(observations),
            )
            for day in horizon
        ]

    values = [observation.value for observation in observations]
    overall_median = _median(values)
    overall_mad = _mad(values, overall_median)

    buckets: dict[int, list[decimal.Decimal]] = {}
    for observation in observations:
        buckets.setdefault(observation.date.weekday(), []).append(observation.value)

    points: list[ForecastPoint] = []
    for day in horizon:
        bucket = buckets.get(day.weekday(), [])
        if len(bucket) >= MIN_BUCKET_OBSERVATIONS:
            centre = _median(bucket)
            spread = _mad(bucket, centre)
            method = ForecastMethod.SEASONAL_DOW_MEDIAN
            count = len(bucket)
        else:
            # Not enough same-weekday history: fall back to the whole window and say so,
            # rather than dressing one observation up as a seasonal estimate.
            centre = overall_median
            spread = overall_mad
            method = ForecastMethod.OVERALL_MEDIAN
            count = len(values)

        half_width = spread * MAD_TO_SIGMA * CONFIDENCE_Z
        points.append(
            ForecastPoint(
                date=day,
                value=centre,
                # Clamped at zero: a negative number of room nights, or negative room
                # revenue on a night, is not a thing the interval should suggest.
                lower=max(ZERO, centre - half_width),
                upper=centre + half_width,
                method=method,
                observations=count,
            )
        )
    return points


def detect_anomalies(
    observations: list[Observation],
    *,
    threshold: decimal.Decimal = ANOMALY_THRESHOLD,
    minimum_observations: int = MIN_TRAINING_OBSERVATIONS,
) -> list[Anomaly]:
    """Flag observations whose modified z-score exceeds *threshold*.

    ``score = 0.6745 * (value - median) / MAD``

    Returns an empty list -- never a fabricated flag -- when the series is too short or when
    the MAD is zero. A MAD of zero means the series has never varied, and a value cannot be
    called unusual against a history with no notion of usual spread.

    Results are ordered by date, so the same input always produces the same output.
    """
    if len(observations) < minimum_observations:
        return []

    values = [observation.value for observation in observations]
    centre = _median(values)
    spread = _mad(values, centre)
    if spread == ZERO:
        return []

    anomalies: list[Anomaly] = []
    for observation in sorted(observations, key=lambda item: item.date):
        deviation = observation.value - centre
        score = MODIFIED_Z_CONSTANT * deviation / spread
        if abs(score) > threshold:
            anomalies.append(
                Anomaly(
                    date=observation.date,
                    value=observation.value,
                    median=centre,
                    deviation=spread,
                    score=score,
                    direction="above" if deviation > ZERO else "below",
                )
            )
    return anomalies


def measure_trend(
    observations: list[Observation],
    *,
    threshold: decimal.Decimal = TREND_THRESHOLD,
    minimum_observations: int = MIN_TRAINING_OBSERVATIONS,
) -> TrendResult:
    """Classify direction by comparing the medians of the window's two halves.

    Chosen over a fitted slope because it is robust to a single outlying day and because the
    operator can check it: both medians and the threshold are returned, so the classification
    is recomputable by hand.

    An odd-length window gives the extra day to the recent half, which is the half a reader
    cares more about.
    """
    ordered = sorted(observations, key=lambda item: item.date)
    if len(ordered) < minimum_observations:
        return TrendResult(
            direction="insufficient_data",
            earlier_median=None,
            recent_median=None,
            relative_change=None,
            observations=len(ordered),
        )

    split = len(ordered) // 2
    earlier = _median([item.value for item in ordered[:split]])
    recent = _median([item.value for item in ordered[split:]])

    if earlier == ZERO:
        # A relative change from zero is undefined. Direction still follows the absolute
        # move, which is the honest reading of "it was nothing, now it is something".
        direction = "increasing" if recent > ZERO else "stable"
        return TrendResult(
            direction=direction,
            earlier_median=earlier,
            recent_median=recent,
            relative_change=None,
            observations=len(ordered),
        )

    change = (recent - earlier) / abs(earlier)
    if change > threshold:
        direction = "increasing"
    elif change < -threshold:
        direction = "decreasing"
    else:
        direction = "stable"

    return TrendResult(
        direction=direction,
        earlier_median=earlier,
        recent_median=recent,
        relative_change=change,
        observations=len(ordered),
    )


__all__ = [
    "ANOMALY_THRESHOLD",
    "CONFIDENCE_LEVEL",
    "CONFIDENCE_Z",
    "MAD_TO_SIGMA",
    "MIN_BUCKET_OBSERVATIONS",
    "MIN_TRAINING_OBSERVATIONS",
    "MODEL_NAME",
    "MODEL_VERSION",
    "MODIFIED_Z_CONSTANT",
    "TREND_THRESHOLD",
    "Anomaly",
    "ForecastMethod",
    "ForecastPoint",
    "Observation",
    "TrendResult",
    "detect_anomalies",
    "forecast_series",
    "measure_trend",
]
