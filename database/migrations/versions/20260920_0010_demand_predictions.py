"""add the demand_predictions table

Strictly additive, and authorised for exactly this change: **one new table** and its two
indexes. Nothing on ``hotels``, ``bookings``, ``audit_events`` or any other table is altered,
and migrations 0001-0009 are untouched.

Stage 6.6 served the demand model and Stage 6.7 put it inside the production image. Neither
kept what it said. A prediction was computed, returned and forgotten, so there was no way to
ask what the platform told a hotel last Tuesday, and no data on which any later drift or
accuracy work could rest. This is that record, and the roadmap's standing V2 rule -- predictions
persisted with the model version that produced them -- is what it discharges.

## The unique constraint is the whole design

``uq_demand_predictions_identity`` covers ``(hotel_id, target_date, forecast_horizon_days,
model_version, feature_digest)``, and the last column is the one that matters. The obvious four
describe what a prediction is *about*; they do not describe what it was computed *from*. A
booking recorded late changes ``demand_lag_7``, so those four can legitimately name two
different numbers on two different days -- and a key without the inputs would force a choice
between overwriting history and refusing a legitimate new prediction.

With the digest in the key, a repeat of the same request collides and writes nothing, and a
request whose inputs have moved becomes a new row beside the old one rather than instead of it.

**The database is the authority, not the application.** The service inserts with ``ON CONFLICT
DO NOTHING`` against this constraint, which is race-safe by construction: two concurrent
identical requests produce one row without either of them reading first and hoping. A
SELECT-then-INSERT would have a window between the two statements; this has none.

## No append-only trigger, unlike audit_events

Deliberate, and the difference is worth stating. An audit row is evidence about a person's
action and must survive a determined edit, which is why migration 0007 installs a trigger. A
prediction is a machine's output, and its integrity is already pinned by ``feature_digest``: a
row edited in place stops matching its own digest and is detectable as edited. Guarding the
weaker claim with the heavier mechanism would buy nothing and would make ``TRUNCATE`` semantics
one more thing to reason about in the test suite's teardown.

## No updated_at and no set_updated_at trigger

Every other table in this schema has both. A row that is never updated has no moment of last
update -- the same reasoning ``audit_events`` records. ``generated_at`` is the only time this
table keeps, and it is defaulted by the database so no caller can influence it.

## ON DELETE RESTRICT

The policy this schema already uses for historical records -- ``bookings -> hotels``,
``payments -> bookings``. A property with predictions cannot be deleted out from under them.
The consequence is stated rather than hidden, and it composes with what the schema already
does: any property with bookings, guests, rooms, revenue or expenses is already undeletable.

Revision ID: 0010_demand_predictions
Revises: 0009_audit_booking_deleted
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010_demand_predictions"
down_revision: str | None = "0009_audit_booking_deleted"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE demand_predictions (
            id BIGINT GENERATED ALWAYS AS IDENTITY NOT NULL,
            hotel_id BIGINT NOT NULL,
            target_date DATE NOT NULL,
            forecast_horizon_days INTEGER NOT NULL,
            prediction_cutoff TIMESTAMP WITH TIME ZONE NOT NULL,
            predicted_room_nights DOUBLE PRECISION NOT NULL,
            model_name TEXT NOT NULL,
            model_version TEXT NOT NULL,
            feature_version TEXT NOT NULL,
            dataset_version TEXT NOT NULL,
            canonical_model_digest TEXT NOT NULL,
            feature_values JSONB NOT NULL,
            feature_digest TEXT NOT NULL,
            request_id TEXT,
            generated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_demand_predictions PRIMARY KEY (id),
            CONSTRAINT uq_demand_predictions_identity UNIQUE (
                hotel_id, target_date, forecast_horizon_days, model_version, feature_digest
            ),
            CONSTRAINT fk_demand_predictions_hotel_id_hotels
                FOREIGN KEY (hotel_id) REFERENCES hotels (id) ON DELETE RESTRICT,
            CONSTRAINT ck_demand_predictions_forecast_horizon_days_positive
                CHECK (forecast_horizon_days >= 1),
            CONSTRAINT ck_demand_predictions_request_id_shape
                CHECK (request_id IS NULL OR request_id ~ '^[A-Za-z0-9._-]{1,64}$'),
            CONSTRAINT ck_demand_predictions_feature_values_object
                CHECK (jsonb_typeof(feature_values) = 'object')
        )
        """
    )

    # The per-hotel history a future read path would page through. Also the index that makes a
    # tenant-scoped lookup a lookup rather than a scan of every hotel's predictions.
    op.execute(
        "CREATE INDEX ix_demand_predictions_hotel_id_target_date "
        "ON demand_predictions (hotel_id, target_date)"
    )
    # The observability query: how many predictions a model version served, and when.
    op.execute(
        "CREATE INDEX ix_demand_predictions_model_version_generated_at "
        "ON demand_predictions (model_version, generated_at)"
    )


def downgrade() -> None:
    # The indexes and constraints go with the table. There is no trigger and no function to
    # drop separately, which is the one way this differs from 0007.
    op.execute("DROP TABLE IF EXISTS demand_predictions")
