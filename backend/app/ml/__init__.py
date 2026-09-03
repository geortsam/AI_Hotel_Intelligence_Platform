"""Statistical models, kept deliberately free of database and HTTP concepts.

Everything in this package takes plain dated observations and returns plain results. There is
no SQLAlchemy here, no session, no request -- which is what makes the models testable without
a database and reusable by anything that can produce a series.

The methods are simple on purpose. A hotel's daily series is short, strongly weekly-seasonal
and full of legitimate zeros; a heavyweight learner fitted to a few dozen points would be
less accurate than a robust baseline and far harder to explain to the operator who has to act
on it. See ``docs/ml-design.md`` for the reasoning and the limitations.
"""

from app.ml.timeseries import (
    ANOMALY_THRESHOLD,
    CONFIDENCE_LEVEL,
    MIN_BUCKET_OBSERVATIONS,
    MIN_TRAINING_OBSERVATIONS,
    MODEL_NAME,
    MODEL_VERSION,
    TREND_THRESHOLD,
    Anomaly,
    ForecastMethod,
    ForecastPoint,
    Observation,
    TrendResult,
    detect_anomalies,
    forecast_series,
    measure_trend,
)

__all__ = [
    "ANOMALY_THRESHOLD",
    "CONFIDENCE_LEVEL",
    "MIN_BUCKET_OBSERVATIONS",
    "MIN_TRAINING_OBSERVATIONS",
    "MODEL_NAME",
    "MODEL_VERSION",
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
