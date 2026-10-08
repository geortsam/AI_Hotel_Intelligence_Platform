"""scope a review's external identifier to its hotel

Authorised for exactly this: **one unique index replaced by another**. No table, column,
constraint, foreign key or other index is created, altered or dropped, and no row is written.

**The defect (Issue H4).** Migration 0001 declared

    uq_reviews_source_external_review_id
        ON reviews (source, external_review_id) WHERE external_review_id IS NOT NULL

with no hotel column, so the pair was unique across the whole platform. One property recording
a platform's review identifier stopped every other property from recording the same pair -- and
the refusal told the second property that something it cannot see already exists. Hotels are
tenants; nothing else in this schema couples two of them.

**The correction.** The index is replaced by

    uq_reviews_hotel_source_external_review_id
        ON reviews (hotel_id, source, external_review_id) WHERE external_review_id IS NOT NULL

The predicate is unchanged: a review with no external identifier is still unconstrained by it.
A hotel still cannot record the same platform reference twice for one source.

**Existing rows.** Every row that satisfied the global key satisfies the per-hotel one, because
the new key is the old one plus a column. ``upgrade`` still counts the rows the new key would
refuse before building it, so a database that somehow holds them stops with a sentence rather
than a driver error. No backfill.

**Downgrade** restores 0001's exact definition. It cannot succeed once two hotels hold the same
``(source, external_review_id)`` -- the global key is stricter than the data -- so it counts those
first and refuses by name instead of half-applying.

Revision ID: 0019_review_external_id_scope
Revises: 0018_composite_set_null_columns
Create Date: 2026-10-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_review_external_id_scope"
down_revision: str | None = "0018_composite_set_null_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The partial predicate both definitions share.
PREDICATE = "external_review_id IS NOT NULL"

#: 0001's global key, and the per-hotel key that replaces it.
GLOBAL_INDEX = "uq_reviews_source_external_review_id"
GLOBAL_COLUMNS = "source, external_review_id"
TENANT_INDEX = "uq_reviews_hotel_source_external_review_id"
TENANT_COLUMNS = "hotel_id, source, external_review_id"


def _duplicates(columns: str) -> int:
    """How many ``(columns)`` groups hold more than one row with an external identifier."""
    return int(
        op.get_bind()
        .execute(
            sa.text(
                f"SELECT count(*) FROM (SELECT 1 FROM reviews WHERE {PREDICATE} "
                f"GROUP BY {columns} HAVING count(*) > 1) AS duplicate_groups"
            )
        )
        .scalar_one()
    )


def _replace(*, drop: str, create: str, columns: str) -> None:
    """Build the new key before dropping the old one: no moment is left unconstrained."""
    op.execute(f"CREATE UNIQUE INDEX {create} ON reviews ({columns}) WHERE {PREDICATE}")
    op.execute(f"DROP INDEX {drop}")


def upgrade() -> None:
    found = _duplicates(TENANT_COLUMNS)
    if found:
        raise RuntimeError(
            f"0019 refused: {found} (hotel_id, source, external_review_id) group(s) already "
            "hold more than one review. Nothing was changed."
        )
    _replace(drop=GLOBAL_INDEX, create=TENANT_INDEX, columns=TENANT_COLUMNS)


def downgrade() -> None:
    found = _duplicates(GLOBAL_COLUMNS)
    if found:
        raise RuntimeError(
            f"0019 downgrade refused: {found} (source, external_review_id) pair(s) are recorded "
            "by more than one review, which the global key 0001 declared cannot hold. Nothing "
            "was changed."
        )
    _replace(drop=TENANT_INDEX, create=GLOBAL_INDEX, columns=GLOBAL_COLUMNS)
