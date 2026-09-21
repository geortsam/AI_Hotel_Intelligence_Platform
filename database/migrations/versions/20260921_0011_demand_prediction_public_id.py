"""add demand_predictions.public_id

Strictly additive, and authorised for exactly this change: **one new column** on one existing
table, plus its unique constraint. No table is created or dropped, no existing column is
altered, and migrations 0001-0010 are untouched.

Stage 6.8 recorded why this column was withheld rather than forgotten:

    "The repository gives a public UUID to entities addressable in a URL. Stage 6.8 adds no
    endpoint, so there is nothing to address. A future read API adds the column in its own
    migration rather than this stage guessing the shape of one."

Stage 6.11 is that read API, so this is that migration.

## The backfill is a table rewrite, and that is worth stating

``gen_random_uuid()`` is **volatile**, so PostgreSQL evaluates it once per existing row rather
than storing one constant in the catalogue. That means this is deliberately *not* the fast-default
case: the table is rewritten, under ``ACCESS EXCLUSIVE``, and every existing row receives its own
distinct value in the same statement. Adding the unique constraint then scans it again to build
the backing index.

On the demo and test databases that is instant. On a large production table it would not be, and
an operator planning a deployment should read it as a rewrite rather than as a catalogue change.
There is no production deployment of this platform today; this note exists so the first one is not
surprised.

The alternative -- add nullable, backfill in batches, then ``SET NOT NULL`` -- trades one lock for
a longer window in which the column means nothing and a later step can be forgotten. For a table
this size, at this stage, the honest single statement is better than the machinery.

## The UUID is a surrogate, not a fingerprint

``public_id`` identifies a row to the outside world. It is **not** derived from the row's content,
so two environments holding the same logical predictions will hold different ``public_id`` values,
and that is correct rather than a defect. The invariants are NOT NULL, UNIQUE, and distinct for
every row -- not reproducibility.

That is the opposite of ``feature_digest`` and ``canonical_model_digest`` on this same table, both
of which are content-derived and *must* agree across environments. Three identifier columns, two
kinds of guarantee; the difference is deliberate.

## No new index

The Stage 6.11 endpoint filters on ``(hotel_id, target_date)``, which
``ix_demand_predictions_hotel_id_target_date`` already serves, and orders within that by
``generated_at`` and ``public_id``. The unique constraint's own index is the only one this
migration adds, and it exists to enforce uniqueness rather than to serve a query.

Revision ID: 0011_demand_prediction_public_id
Revises: 0010_demand_predictions
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011_demand_prediction_public_id"
down_revision: str | None = "0010_demand_predictions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # One statement does the add, the default and the backfill: a volatile default is evaluated
    # per row, so every existing row gets its own UUID here rather than in a second pass.
    op.execute(
        """
        ALTER TABLE demand_predictions
            ADD COLUMN public_id UUID DEFAULT gen_random_uuid() NOT NULL
        """
    )
    op.execute(
        """
        ALTER TABLE demand_predictions
            ADD CONSTRAINT uq_demand_predictions_public_id UNIQUE (public_id)
        """
    )


def downgrade() -> None:
    # The constraint first: dropping the column would take its index with it, but naming both
    # keeps the reversal readable as the exact inverse of the upgrade.
    op.execute(
        "ALTER TABLE demand_predictions DROP CONSTRAINT IF EXISTS uq_demand_predictions_public_id"
    )
    op.execute("ALTER TABLE demand_predictions DROP COLUMN IF EXISTS public_id")
