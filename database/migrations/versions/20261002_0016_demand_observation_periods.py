"""create demand_observation_periods, the declared spans for which a hotel's demand is observed

Authorised for exactly this: **one new table**, its constraints and nothing else. No existing
table is altered, and no existing constraint, index or trigger is touched.

**What a row says.** For one hotel, every date from ``observed_from`` to ``observed_to`` -- both
inclusive -- is a date whose complete booking record is held in this database. It is a
DECLARATION, made by an operator (``python -m app.jobs.demand_observation``), and it is the only
evidence of observation the schema has: neither ``booking_room_nights`` nor any creation
timestamp can prove that a date with no occupied nights was observed rather than unrecorded.
Inside a declared period a date with no occupied nights therefore has a demand of zero; outside
every period a date's demand is unknown, whatever was recorded for it.

**Closed on both ends, always.** ``observed_to`` is NOT NULL: an open-ended period would turn
every future day into an observed zero the moment it passed, without anyone having said so.
Observation is extended by declaring the next span; a stale declaration then fails safe -- the
days after it read as unknown, never as zero.

**Several per hotel, never overlapping.** A gap in the record (an outage, an import that left a
hole) is represented by not declaring it, which needs more than one span. The exclusion
constraint makes two spans that share a date impossible, so "is this date observed?" has one
answer. Adjacent spans are allowed: ``[a, b]`` and ``[b + 1, c]`` share no date.

**No updated_at.** A span is declared or withdrawn, never edited.

``ON DELETE CASCADE`` to ``hotels``: a statement about a hotel's data cannot outlive the hotel.

Revision ID: 0016_demand_observation_periods
Revises: 0015_copilot_conversations
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0016_demand_observation_periods"
down_revision: str | None = "0015_copilot_conversations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # btree_gist, which lets one GiST exclusion constraint combine `=` on hotel_id with `&&` on
    # the date range, was installed by 0001 for the room-overlap constraint.
    op.execute(
        """
        CREATE TABLE demand_observation_periods (
            id BIGINT GENERATED ALWAYS AS IDENTITY NOT NULL,
            hotel_id BIGINT NOT NULL,
            observed_from DATE NOT NULL,
            observed_to DATE NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_demand_observation_periods PRIMARY KEY (id),
            CONSTRAINT fk_demand_observation_periods_hotel_id_hotels
                FOREIGN KEY (hotel_id) REFERENCES hotels (id) ON DELETE CASCADE,
            CONSTRAINT ck_demand_observation_periods_ordered
                CHECK (observed_to >= observed_from),
            CONSTRAINT excl_demand_observation_periods_no_overlap
                EXCLUDE USING gist (
                    hotel_id WITH =,
                    daterange(observed_from, observed_to, '[]') WITH &&
                )
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS demand_observation_periods")
