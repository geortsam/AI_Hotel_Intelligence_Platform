"""What a stored demand prediction looks like when a hotel reads its own history.

Stage 6.11. Ten fields, and the list is exhaustive rather than a starting point.

**No internal identifier travels.** The row's ``BIGINT`` primary key and its ``hotel_id`` stay
inside the process; ``public_id`` exists precisely so that neither has to leave. A test asserts
the property set exactly, so an eleventh field cannot arrive without someone choosing it.

## What is withheld, and why

``feature_values`` and ``feature_digest`` -- this endpoint discloses *what the platform said*,
not the internal feature vector it said it from. A hotel reading its own forecast history is
owed the number and its provenance; the nine model inputs are a different disclosure with a
different argument behind it, and Stage 6.11 does not make that argument.

``canonical_model_digest`` -- ``model_version`` is the label published in the model card, which
is what identifies the model to a reader. The digest is an internal identity fingerprint used by
the build to refuse the wrong artifact; it means nothing outside that check.

``request_id`` -- it correlates a row with the server's own logs. That is an operator's tool,
not a tenant's.

None of these is a permanent exclusion. They are what *this* stage discloses, and a later stage
that wants more has to say why.

## What the numbers do not mean

The four version fields and ``prediction_cutoff`` travel so a number can be attributed and
reproduced -- the same reason they ride on the Stage 6.6 forecast response. They are not a
quality claim. **This endpoint establishes no accuracy, endorses no individual prediction, and
detects nothing**; the model is an offline research candidate whose production accuracy is not
established, and which Stage 6.6 measured cannot distinguish hotels below roughly forty room
nights a night.
"""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, Field


class StoredDemandPredictionResponse(BaseModel):
    """One prediction this hotel was served, as it was stored."""

    model_config = ConfigDict(frozen=True)

    public_id: uuid.UUID = Field(
        description=(
            "Public identifier of this prediction. A surrogate, not a fingerprint: it is not "
            "derived from the row, so the same logical prediction carries different values in "
            "different environments."
        )
    )
    target_date: dt.date = Field(description="The day whose occupied room nights were predicted.")
    forecast_horizon_days: int = Field(description="How far ahead the forecast reached, in days.")
    prediction_cutoff: dt.datetime = Field(
        description=(
            "The instant a fact had to precede to be an input. The leakage boundary this "
            "prediction claims."
        )
    )
    predicted_room_nights: float = Field(
        description=(
            "The number the model produced, at full precision. A regression output, not a "
            "guarantee: no production accuracy is established for this model."
        )
    )

    model_name: str = Field(description="The model that produced it.")
    model_version: str = Field(description="Its version, as published in the model card.")
    feature_version: str = Field(description="The feature contract version it was computed under.")
    dataset_version: str = Field(description="The dataset version the model was fitted on.")

    generated_at: dt.datetime = Field(
        description=(
            "When the prediction was produced and stored. Database-assigned, so a repeat of an "
            "identical request does not move it."
        )
    )


__all__ = ["StoredDemandPredictionResponse"]
