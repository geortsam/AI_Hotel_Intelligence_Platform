"""add public_id to payments

Strictly additive, and authorised for exactly this change.

``payments`` was the only addressable domain table with no public identifier and no usable
natural key: its single unique index

    CREATE UNIQUE INDEX uq_payments_provider_transaction_reference
      ON payments (provider, transaction_reference) WHERE transaction_reference IS NOT NULL

is *partial* and spans two *nullable* columns, so a cash, voucher or OTA-collect payment has
no unique identifier at all. That blocked two things: addressing an individual payment, and
-- more seriously -- letting a refund name the charge it reverses, which
``ck_payments_refund_references_charge`` requires.

This migration adds ``public_id`` and its uniqueness. It removes and alters nothing: every
existing column, constraint, index, foreign key and trigger on ``payments`` is untouched, and
no other table is modified.

Note on the rewrite: ``gen_random_uuid()`` is a volatile default, so PostgreSQL rewrites the
table and assigns each existing row its own distinct UUID -- which is what the uniqueness
constraint needs. On a large table this is not an online operation.

Revision ID: 0002_payments_public_id
Revises: 0001_initial_schema
Create Date: 2026-08-28
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_payments_public_id"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NOT NULL with a volatile default: existing rows each receive a fresh UUID.
    op.execute(
        """
        ALTER TABLE payments
            ADD COLUMN public_id UUID NOT NULL DEFAULT gen_random_uuid()
        """
    )
    op.execute(
        """
        ALTER TABLE payments
            ADD CONSTRAINT uq_payments_public_id UNIQUE (public_id)
        """
    )


def downgrade() -> None:
    # Reverses exactly what upgrade() added, in the opposite order. Dropping the constraint
    # first is not strictly required -- dropping the column would take it -- but being
    # explicit keeps the reversal readable and its intent unambiguous.
    op.execute("ALTER TABLE payments DROP CONSTRAINT IF EXISTS uq_payments_public_id")
    op.execute("ALTER TABLE payments DROP COLUMN IF EXISTS public_id")
