"""The offline inference contract: a verified artifact in, typed predictions out.

Deliberately small, and deliberately unhelpful. Every convenience a serving layer would offer --
filling a missing feature, re-ordering columns to match, coercing a string to a float, computing
a lag the caller forgot -- is a way for a prediction to be produced from inputs that are not the
ones the model was fitted on. This module refuses all of them by name.

What it will not do, stated so the absences are visible:

* **no database.** It imports no SQLAlchemy, opens no session and knows no connection string.
* **no network.** It makes no HTTP request and reads no URL.
* **no feature engineering.** It computes nothing. The caller supplies the exact feature vector
  the Stage 6.1 contract defines, and anything else is an error rather than an input to fix.
* **no silent filling or re-ordering.** A missing column, an extra column and a permuted column
  list are three different errors with three different messages.
* **no training.** ``fit``, ``fit_predict`` and ``partial_fit`` are never called; a test
  monkey-patches ``fit`` to raise and requires inference to succeed anyway.
* **no mutation.** The artifact is read; nothing here writes one.

Predictions come back as :class:`DemandPrediction`, keyed by the hotel's **public UUID**.
Internal ``BIGINT`` keys have no field to travel in -- a test asserts the dataclass's field list.
"""

from __future__ import annotations

import datetime as dt
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ml.artifact import LoadedArtifact
from ml.models import MODEL_VERSION

EXPECTED_FEATURE_VERSION = "v1"


class InferenceError(Exception):
    """The request does not match the artifact. No prediction is produced."""


@dataclass(frozen=True, slots=True)
class FeatureVector:
    """One row to score: who, when, at what horizon, and the exact features.

    ``features`` is order-sensitive. A plain ``dict`` preserves insertion order, which is what
    makes "the caller handed us the columns in the wrong order" a detectable mistake rather than
    an invisible one.
    """

    hotel_public_id: uuid.UUID
    target_date: dt.date
    forecast_horizon_days: int
    features: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class DemandPrediction:
    """One prediction, carrying enough identity to be attributable later.

    The model version travels with the number on purpose: a prediction without the version that
    produced it cannot be reproduced, and cannot be invalidated when the model changes.
    """

    hotel_public_id: uuid.UUID
    target_date: dt.date
    model_version: str
    forecast_horizon_days: int
    prediction: float


def _validate_columns(given: Sequence[str], expected: Sequence[str]) -> None:
    """Names first, then order. Three failures, three messages."""
    if tuple(given) == tuple(expected):
        return
    missing = [name for name in expected if name not in given]
    unexpected = [name for name in given if name not in expected]
    if missing or unexpected:
        raise InferenceError(
            f"feature columns do not match the artifact: missing {missing}, unexpected {unexpected}"
        )
    raise InferenceError(
        "feature columns are the artifact's but in a different order; they are consumed "
        f"positionally, so the order is part of the contract. Expected {list(expected)}, got "
        f"{list(given)}"
    )


def _validate_value(name: str, value: object, where: str) -> float:
    """A number, and a finite one. ``bool`` is rejected rather than quietly read as 0 or 1."""
    if isinstance(value, bool):
        raise InferenceError(
            f"{where}: feature {name} is a bool; features are measurements and must be int or float"
        )
    if not isinstance(value, int | float):
        raise InferenceError(
            f"{where}: feature {name} is {type(value).__name__}, expected int or float"
        )
    number = float(value)
    if math.isnan(number):
        raise InferenceError(f"{where}: feature {name} is NaN; no value is filled in for it")
    if math.isinf(number):
        raise InferenceError(f"{where}: feature {name} is infinite")
    return number


def _validate_artifact(
    artifact: LoadedArtifact,
    *,
    expected_model_version: str,
    expected_feature_version: str,
) -> None:
    if artifact.model_version != expected_model_version:
        raise InferenceError(
            f"artifact is {artifact.model_version!r}, this call expects {expected_model_version!r}"
        )
    if artifact.feature_version != expected_feature_version:
        raise InferenceError(
            f"artifact was built against feature_version {artifact.feature_version!r}, this "
            f"call expects {expected_feature_version!r}"
        )
    if not artifact.feature_columns:
        raise InferenceError("the artifact declares no feature columns")


def predict_demand(
    artifact: LoadedArtifact,
    rows: Sequence[FeatureVector],
    *,
    expected_model_version: str = MODEL_VERSION,
    expected_feature_version: str = EXPECTED_FEATURE_VERSION,
) -> tuple[DemandPrediction, ...]:
    """Score an already-validated artifact against an exactly-matching feature matrix.

    Deterministic: the same artifact and the same rows produce the same numbers, in the same
    order, on every call. The estimator is asked once for the whole batch rather than row by
    row -- a per-row call would change the floating-point summation order inside the ensemble
    and make "deterministic" quietly conditional on batch size.
    """
    _validate_artifact(
        artifact,
        expected_model_version=expected_model_version,
        expected_feature_version=expected_feature_version,
    )
    if not rows:
        return ()

    columns = artifact.feature_columns
    matrix: list[list[float]] = []
    for index, row in enumerate(rows):
        where = f"row {index} ({row.hotel_public_id} on {row.target_date})"
        if not isinstance(row.hotel_public_id, uuid.UUID):
            raise InferenceError(f"{where}: hotel_public_id must be a UUID")
        if not isinstance(row.target_date, dt.date) or isinstance(row.target_date, dt.datetime):
            raise InferenceError(f"{where}: target_date must be a date, not a datetime")
        if row.forecast_horizon_days != artifact.forecast_horizon_days:
            raise InferenceError(
                f"{where}: horizon {row.forecast_horizon_days} does not match the artifact's "
                f"{artifact.forecast_horizon_days}"
            )
        _validate_columns(tuple(row.features), columns)
        matrix.append([_validate_value(name, row.features[name], where) for name in columns])

    raw = artifact.estimator.predict(matrix)
    if len(raw) != len(rows):
        raise InferenceError(f"the estimator returned {len(raw)} value(s) for {len(rows)} row(s)")
    return tuple(
        DemandPrediction(
            hotel_public_id=row.hotel_public_id,
            target_date=row.target_date,
            model_version=artifact.model_version,
            forecast_horizon_days=artifact.forecast_horizon_days,
            prediction=float(value),
        )
        for row, value in zip(rows, raw, strict=True)
    )
