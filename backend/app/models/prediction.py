"""Production demand predictions: what the model said, about what, and from which inputs.

Stage 6.8. One row per prediction the API has served, written inside the transaction that
served it. Until this stage a prediction was computed, returned and forgotten; there was no way
to ask what the platform told a hotel last Tuesday, and no data on which any later drift or
accuracy work could be built.

**Append-only in practice, though not by trigger.** There is no update method, no delete method
and no endpoint for either. Unlike ``audit_events`` this table carries no database trigger
enforcing that, and the reason is the deliberate difference between the two: an audit row is
evidence about a person's action and must survive a determined edit, while a prediction is a
machine's output whose integrity is already pinned by ``feature_digest`` -- a row edited in
place stops matching its own digest and is detectable as edited. Adding a trigger would be
protecting the weaker claim with the heavier mechanism.

**No ``updated_at``.** Every other table in this schema has one. A row that is never updated has
no moment of last update, which is the same reasoning ``audit_events`` records.

**The identity is the inputs, not only the subject.** See :data:`IDENTITY_COLUMNS`.

**One tenant per row, never null.** ``hotel_id`` is ``ON DELETE RESTRICT``, the policy this
schema already uses for historical records -- ``bookings -> hotels``, ``payments -> bookings``
-- so a property with predictions cannot be deleted out from under them.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, pk_column
from app.models.audit import REQUEST_ID_SQL_PATTERN

#: What makes two predictions the same prediction.
#:
#: Not the obvious four. Hotel, target date, horizon and model version describe what a
#: prediction is *about*; they do not describe what it was computed *from*. A booking recorded
#: late changes ``demand_lag_7``, so those four can legitimately name two different numbers on
#: two different days, and keying on them alone would force a choice between overwriting history
#: and refusing a legitimate new prediction.
#:
#: ``feature_digest`` closes that: a repeat of the same request collides and writes nothing, and
#: a request whose inputs have moved is a new row beside the old one rather than instead of it.
IDENTITY_COLUMNS = (
    "hotel_id",
    "target_date",
    "forecast_horizon_days",
    "model_version",
    "feature_digest",
)

#: The constraint the database enforces that identity with. Named here because the repository
#: has to hand the name to ``ON CONFLICT``, and a literal spelled in two places is a literal
#: that eventually disagrees with itself.
IDENTITY_CONSTRAINT = "uq_demand_predictions_identity"


class DemandPrediction(Base):
    """One prediction served by `GET /hotels/{id}/ml/demand-forecast`."""

    __tablename__ = "demand_predictions"

    id: Mapped[int] = pk_column()

    #: How a prediction is named outside this process (Stage 6.11).
    #:
    #: Stage 6.8 withheld this column and said why: "Stage 6.8 adds no endpoint, so there is
    #: nothing to address. A future read API adds the column in its own migration rather than
    #: this stage guessing the shape of one." Migration 0011 is that migration.
    #:
    #: A surrogate, not a fingerprint. It is not derived from the row, so two environments
    #: holding the same logical predictions hold different values here -- deliberately, and in
    #: deliberate contrast with ``feature_digest`` and ``canonical_model_digest`` on this same
    #: table, both of which are content-derived and must agree everywhere. The BIGINT ``id``
    #: above never leaves this process; this is what does.
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()")
    )

    #: The tenant. There is no row belonging to no hotel, and no nullable tenant column to get
    #: wrong. The hotel's PUBLIC id is deliberately not denormalised here: it would be a second
    #: source of tenant identity, and RESTRICT guarantees the join always resolves.
    hotel_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("hotels.id", ondelete="RESTRICT"), nullable=False
    )

    #: The day whose occupied room nights were predicted.
    target_date: Mapped[dt.date] = mapped_column(Date, nullable=False)

    #: How far ahead, in whole days. Stored rather than assumed: a second horizon would
    #: otherwise silently share rows with the first.
    forecast_horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)

    #: The instant a fact had to precede to be an input -- the leakage boundary this row
    #: CLAIMS. Recorded rather than re-derived later from a horizon that may by then mean
    #: something else.
    prediction_cutoff: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    #: The regression output, at full precision. Not ``NUMERIC``: this is not money, and
    #: rounding it would invent a precision the model does not have.
    predicted_room_nights: Mapped[float] = mapped_column(Float(precision=53), nullable=False)

    # --- the model that produced it ---------------------------------------------------------
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    model_version: Mapped[str] = mapped_column(Text, nullable=False)
    feature_version: Mapped[str] = mapped_column(Text, nullable=False)
    dataset_version: Mapped[str] = mapped_column(Text, nullable=False)

    #: The model IDENTITY -- the Stage 6.5 canonical digest, which is stable across
    #: environments. The artifact's payload SHA-256 is deliberately absent: Stage 6.7 measured
    #: that it changes with the build machine's OpenMP thread count, so storing it would record
    #: which CPU served the request rather than anything about the prediction.
    canonical_model_digest: Mapped[str] = mapped_column(Text, nullable=False)

    # --- the inputs ---------------------------------------------------------------------------

    #: The nine features the prediction was computed from, keyed by name.
    #:
    #: PostgreSQL normalises ``jsonb`` key order, so the ORDER of the columns is carried by
    #: ``feature_digest`` rather than by this object -- which is the right place for it, since
    #: the digest is what a reader recomputes to check the row.
    feature_values: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    #: SHA-256 over the ordered ``[name, value]`` pairs at six decimal places. Part of the
    #: identity, and the reason a row edited in place stops matching itself.
    feature_digest: Mapped[str] = mapped_column(Text, nullable=False)

    # --- provenance -----------------------------------------------------------------------------

    #: The ``X-Request-ID`` of the call that produced it, so a row and the log lines of the
    #: request that caused it can be tied together. Same shape and same CHECK as
    #: ``audit_events.request_id``; nullable for the same reason, a call that carried none.
    request_id: Mapped[str | None] = mapped_column(Text)

    #: When the prediction was produced. Defaulted by the database, so no caller influences it,
    #: and never part of the identity -- a repeat must not move it.
    generated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(*IDENTITY_COLUMNS, name=IDENTITY_CONSTRAINT),
        # Stage 6.11. The public handle a read API addresses a row by.
        UniqueConstraint("public_id", name="uq_demand_predictions_public_id"),
        CheckConstraint("forecast_horizon_days >= 1", name="forecast_horizon_days_positive"),
        CheckConstraint(
            f"request_id IS NULL OR request_id ~ '{REQUEST_ID_SQL_PATTERN}'",
            name="request_id_shape",
        ),
        # jsonb_typeof pins the inputs to an OBJECT. Without it the column would accept a bare
        # string or an array and every reader would have to defend itself.
        CheckConstraint("jsonb_typeof(feature_values) = 'object'", name="feature_values_object"),
        # The per-hotel history a future read path would page through, and what makes a
        # tenant-scoped lookup a lookup rather than a scan of every hotel's predictions.
        Index("ix_demand_predictions_hotel_id_target_date", "hotel_id", "target_date"),
        # The observability query: how many predictions this model version served, and when.
        Index("ix_demand_predictions_model_version_generated_at", "model_version", "generated_at"),
    )


__all__ = ["IDENTITY_COLUMNS", "IDENTITY_CONSTRAINT", "DemandPrediction"]
