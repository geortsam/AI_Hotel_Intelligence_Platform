"""What the forecast-performance endpoints return. The HTTP contract, and only that.

Stage 7.3. Stages 6.9 and 6.10 built two measurement services and deliberately gave them no
endpoint; their results are frozen dataclasses in ``app.schemas.ml_accuracy`` and
``app.schemas.ml_drift``, which are internal shapes and stay internal. This module is the
separate, narrower thing that crosses the network.

**Two shapes, not one reused.** The obvious alternative -- registering the frozen dataclasses as
response models -- would have made every field a measurement produces a published API field,
including the two that must not travel. Keeping a second set of models is the cost of being able
to publish some of a result and withhold the rest, and the withholding is the point.

## What is withheld, and why

``canonical_model_digest`` -- present on every internal result, on none of these. It is the
build's fingerprint for refusing the wrong artifact; outside that check it means nothing, and
``model_version`` is the label the model card publishes to identify the model to a reader. This
is Stage 6.11's rule for the same field, applied again rather than re-argued.

``scored_feature_digests`` and ``feature_digests`` -- per-prediction attribution, one entry per
scored row. Internally they let any aggregate be traced back to the exact rows behind it, which
is a reason to compute them and not a reason to publish them: over a year's window the list is
the largest thing in the payload and the least usable, and it indexes server-side rows a tenant
cannot address anyway.

## What IS disclosed here that Stage 6.11 withheld, and the argument for it

Stage 6.11 declined to return ``feature_values``, saying the model's nine inputs were "a
different disclosure with a different argument behind it" and that it was not making that
argument. :class:`PredictionDistributionResponse` publishes **summary statistics** over those
inputs, so the argument is owed. It is this:

* six of the nine -- ``day_of_week``, ``day_of_month``, ``month``, ``week_of_year``,
  ``day_of_year``, ``is_weekend`` -- are calendar arithmetic on the target date. They are the
  same for every hotel on Earth and disclose nothing about any of them;
* the other three -- ``demand_lag_7``, ``demand_lag_14``, ``demand_lag_28`` -- are this hotel's
  own realised room nights, which the same caller can already read at the same role from
  ``/analytics/daily``. A quantile of a figure you are already entitled to read in full is not a
  new disclosure;
* ``predicted_room_nights`` is what this hotel was already told, and Stage 6.11 returns it per
  row.

So the set is bounded by what the caller already has, which is why this response is readable by a
member of any role while :class:`ForecastAccuracyResponse` requires a manager. Note the one sharp
edge, which the protocol itself states: a window holding a single prediction reports that
prediction's own value as its minimum, maximum, mean, median and every quantile. The summary of
one observation is the observation. That is disclosure of the hotel's own datum to the hotel's
own member, which is the boundary this argument draws and does not cross.

## The claims boundary travels in the payload

Every response carries :class:`MeasurementMetadata`: the protocol that produced the numbers, its
checksum, ``establishes_production_accuracy = False``, and one sentence saying what that means. A
metric without the rules that produced it is a number looking for a caption, and a consumer that
reads only the body is still told what the body does not establish.

There is no threshold field, no verdict field, no alert, no ranking and no baseline *model*
anywhere here, because the protocols contain none and an endpoint that implied otherwise would be
making a claim its own service refuses to make.
"""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, Field

#: The sentence every forecast-performance response carries, verbatim. A constant so that the
#: claim boundary is one string in one place, and a test can pin it against drift.
MEASUREMENT_STATEMENT = (
    "These figures measure predictions this hotel was served, under a protocol fixed and "
    "checksummed before any number was computed. They establish no production accuracy, "
    "evaluate no threshold, compare against no baseline model, detect nothing and rank nothing. "
    "The served artifact remains an offline research candidate."
)


class MeasurementMetadata(BaseModel):
    """The rules a set of figures was produced under, and what they do not establish."""

    model_config = ConfigDict(frozen=True)

    protocol_version: str = Field(
        description="The protocol these figures were produced under, e.g. `accuracy_v1`."
    )
    protocol_checksum: str = Field(
        description=(
            "SHA-256 over the protocol's own values. Two results carrying the same checksum "
            "were measured by the same rules."
        )
    )
    establishes_production_accuracy: bool = Field(
        description=(
            "Always false. Measuring under a declared protocol is not the same as establishing "
            "that a model is accurate in production."
        )
    )
    statement: str = Field(description="The claims boundary in prose, so the body explains itself.")


# ======================================================================================
# Accuracy
# ======================================================================================


class AccuracyMetrics(BaseModel):
    """One segment's error and its denominator.

    ``null`` rather than ``0.0`` when nothing was scored: a segment with no observations has no
    error, and reporting zero would read as a perfect one.
    """

    model_config = ConfigDict(frozen=True)

    observations: int = Field(ge=0, description="Predictions scored into this segment.")
    skipped: int = Field(
        ge=0,
        description=(
            "Eligible predictions whose target date had no recorded occupancy to score "
            "against. Skipped, never scored as zero."
        ),
    )
    mae: float | None = Field(description="Mean absolute error, in room nights. Null if none.")
    rmse: float | None = Field(description="Root mean squared error, in room nights.")
    smape: float | None = Field(description="Symmetric mean absolute percentage error.")


class SegmentAccuracyResponse(BaseModel):
    """One of the protocol's two declared segments, with its own denominator."""

    model_config = ConfigDict(frozen=True)

    segment: str = Field(description="`below_calibration` or `within_calibration`.")
    metrics: AccuracyMetrics


class ModelVersionAccuracyResponse(BaseModel):
    """One model version's error, split into the two segments and never pooled across them."""

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    model_version: str = Field(description="The version label published in the model card.")
    below_calibration: SegmentAccuracyResponse
    within_calibration: SegmentAccuracyResponse


class ForecastAccuracyResponse(BaseModel):
    """How far off this hotel's served predictions were, over one window, as of one date."""

    model_config = ConfigDict(frozen=True)

    hotel_public_id: uuid.UUID
    as_of_date: dt.date = Field(
        description="The date the measurement was taken as of. The only notion of `when` here."
    )
    window_from: dt.date = Field(description="Earliest target date requested, inclusive.")
    window_to: dt.date = Field(description="Latest target date requested, inclusive.")
    scored_from: dt.date | None = Field(
        description=(
            "Earliest target date actually scored, or null when nothing in the window had "
            "cleared the settlement lag by `as_of_date`."
        )
    )
    scored_to: dt.date | None = Field(
        description="Latest target date actually scored; `window_to` clamped to the lag."
    )

    settlement_lag_days: int = Field(
        ge=0,
        description=(
            "Days a target date must be in the past before it is scored, so that late-arriving "
            "bookings are already recorded."
        ),
    )

    candidates: int = Field(ge=0, description="Predictions the window held, after selection.")
    ineligible_by_settlement: int = Field(
        ge=0, description="Candidates whose target date had not cleared the lag."
    )
    out_of_scope_model_digest: int = Field(
        ge=0,
        description=(
            "Eligible candidates produced by a model identity other than the approved one. "
            "Counted here and pooled into nothing."
        ),
    )

    unsettled_allocations: int = Field(
        ge=0, description="Room allocations covering the scored window that can still change."
    )
    settled: bool = Field(
        description=(
            "False means at least one allocation covering the scored window can still change "
            "status, so these figures are provisional."
        )
    )

    by_model_version: list[ModelVersionAccuracyResponse] = Field(
        description="One entry per model version present. Versions never share a denominator."
    )

    measurement: MeasurementMetadata


# ======================================================================================
# Distribution
# ======================================================================================


class QuantileValue(BaseModel):
    """One quantile of one field. A list, not an object, so the protocol's order is visible."""

    model_config = ConfigDict(frozen=True)

    label: str = Field(description="The protocol's label, e.g. `p05`.")
    value: float | None = Field(description="Null when the series was empty.")


class FieldSummaryResponse(BaseModel):
    """One observed field, summarised over one segment of one window."""

    model_config = ConfigDict(frozen=True)

    field: str = Field(description="A model input column, or `predicted_room_nights`.")
    count: int = Field(ge=0, description="Values summarised. Statistics below are null when zero.")
    minimum: float | None
    maximum: float | None
    mean: float | None
    median: float | None
    quantiles: list[QuantileValue]


class SegmentSummaryResponse(BaseModel):
    """One segment of one window, every observed field summarised in the protocol's order."""

    model_config = ConfigDict(frozen=True)

    segment: str = Field(description="`below_calibration` or `within_calibration`.")
    observations: int = Field(ge=0, description="Predictions in this segment.")
    fields: list[FieldSummaryResponse]


class ModelVersionSummaryResponse(BaseModel):
    """One model version's distributions, never pooled with another's."""

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    model_version: str
    below_calibration: SegmentSummaryResponse
    within_calibration: SegmentSummaryResponse


class WindowSummaryResponse(BaseModel):
    """One window of stored predictions, summarised."""

    model_config = ConfigDict(frozen=True)

    window_from: dt.date
    window_to: dt.date
    candidates: int = Field(ge=0, description="Predictions the window held, after selection.")
    out_of_scope_model_digest: int = Field(
        ge=0, description="Predictions from another model identity, summarised into nothing."
    )
    by_model_version: list[ModelVersionSummaryResponse]


class FieldDifferenceResponse(BaseModel):
    """Target minus baseline, per statistic. Null wherever either side had nothing."""

    model_config = ConfigDict(frozen=True)

    field: str
    count: int = Field(description="Target count minus baseline count. May be negative.")
    minimum: float | None
    maximum: float | None
    mean: float | None
    median: float | None
    quantiles: list[QuantileValue]


class SegmentDifferenceResponse(BaseModel):
    """How far one segment moved between the two windows."""

    model_config = ConfigDict(frozen=True)

    segment: str
    observations: int = Field(description="Target minus baseline. May be negative.")
    fields: list[FieldDifferenceResponse]


class ModelVersionDifferenceResponse(BaseModel):
    """One model version's movement. Present even if it served in only one of the two windows."""

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    model_version: str
    below_calibration: SegmentDifferenceResponse
    within_calibration: SegmentDifferenceResponse


class DistributionComparisonResponse(BaseModel):
    """The baseline window and the movement from it. Absent entirely when none was requested."""

    model_config = ConfigDict(frozen=True)

    baseline: WindowSummaryResponse
    by_model_version: list[ModelVersionDifferenceResponse]


class PredictionDistributionResponse(BaseModel):
    """What this hotel's stored predictions looked like, and optionally how far they moved.

    **This describes; it does not decide.** No threshold is evaluated and no verdict is reached,
    here or in the protocol behind it. A difference in this payload is a difference, not drift.
    """

    model_config = ConfigDict(frozen=True)

    hotel_public_id: uuid.UUID
    observed: WindowSummaryResponse
    comparison: DistributionComparisonResponse | None = Field(
        default=None,
        description=(
            "Present only when a baseline window was requested. Null, never a fabricated set of "
            "zero differences."
        ),
    )

    measurement: MeasurementMetadata


__all__ = [
    "MEASUREMENT_STATEMENT",
    "AccuracyMetrics",
    "DistributionComparisonResponse",
    "FieldDifferenceResponse",
    "FieldSummaryResponse",
    "ForecastAccuracyResponse",
    "MeasurementMetadata",
    "ModelVersionAccuracyResponse",
    "ModelVersionDifferenceResponse",
    "ModelVersionSummaryResponse",
    "PredictionDistributionResponse",
    "QuantileValue",
    "SegmentAccuracyResponse",
    "SegmentDifferenceResponse",
    "SegmentSummaryResponse",
    "WindowSummaryResponse",
]
