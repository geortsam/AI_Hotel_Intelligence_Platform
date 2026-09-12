"""add the audit_events_archive table

Strictly additive, and authorised for exactly this change: **one new table**, its indexes, and
one trigger that guards it. Nothing on ``audit_events`` is altered -- not a column, not a
constraint, and above all not ``trg_audit_events_append_only``. Migrations 0001-0007 are
untouched.

**This migration adds no way to delete an audit event, and that is the design rather than an
omission.** Stage 4.5.14 was asked for retention and told, in the same breath, not to weaken
audit immutability to get it. Those two requirements meet here: ``audit_events`` is append-only
at the database level, so physically removing an archived row would mean dropping or bypassing
the trigger that makes the audit trail evidence. It is not done. What this migration provides
is the archival half -- a durable, verified, immutable copy -- and the record of which events
have been archived. Physical removal, if it is ever wanted, is a separate decision with its own
migration, and it needs the archive to exist first. See :mod:`app.services.retention`.

**The primary key is the original ``audit_events.id``.** Not a surrogate, and not generated:
the identity of an archived event is the identity of the event. That single choice is what
makes the archival job idempotent and concurrency-safe *in the database* rather than in Python
-- a second attempt to archive the same event is refused by ``pk_audit_events_archive``, and
``ON CONFLICT DO NOTHING`` turns that refusal into a no-op, whichever of two concurrent workers
gets there first. ``uq_audit_events_archive_public_id`` guards the same invariant from the
other direction.

**No foreign keys.** ``audit_events`` points at ``hotels`` and ``users`` with ON DELETE
RESTRICT, which is correct there: it stops a property or an account being erased while the
live record of what it did still exists. Repeating those constraints here would buy nothing
today -- the source row still exists, so the RESTRICT still bites -- and would defeat the
archive's purpose the day physical deletion arrives, because an archive that cannot outlive
the operational rows it describes is not evidence. The internal keys are kept as plain values
and ``hotel_public_id`` / ``actor_public_id`` are denormalised beside them, so an archived row
identifies its tenant and its actor with no join and no surviving parent.

**``hotel_id`` stays nullable and is copied verbatim**, so the archive draws exactly the same
hotel/platform line the active table does. A platform-scoped event (password change, global
catalogue edit) archives as a platform-scoped row; it is never given a hotel to make a NOT NULL
constraint happy.

**No CHECK on ``action`` or ``resource_type``.** The active table already refused anything
outside those vocabularies on the way in. A second copy of the list here would be a second
thing to keep in step with the enum, and a stale copy could reject a row this application had
already accepted -- turning a retention job into an outage.

Revision ID: 0008_audit_retention_archive
Revises: 0007_audit_events
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_audit_retention_archive"
down_revision: str | None = "0007_audit_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE audit_events_archive (
            audit_event_id BIGINT NOT NULL,
            public_id UUID NOT NULL,
            hotel_id BIGINT,
            hotel_public_id UUID,
            actor_user_id BIGINT,
            actor_public_id UUID,
            action TEXT NOT NULL,
            resource_type TEXT NOT NULL,
            resource_reference TEXT NOT NULL,
            request_id TEXT,
            details JSONB DEFAULT '{}'::jsonb NOT NULL,
            occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
            archived_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_audit_events_archive PRIMARY KEY (audit_event_id),
            CONSTRAINT uq_audit_events_archive_public_id UNIQUE (public_id)
        )
        """
    )

    # The one access pattern an archive has: "what do we hold from this period?". Ordered like
    # the active listing so both read the same way, with the id breaking ties for the same
    # reason -- events written by one transaction share a timestamp to the microsecond.
    op.execute(
        "CREATE INDEX ix_audit_events_archive_occurred_at "
        "ON audit_events_archive (occurred_at DESC, audit_event_id DESC)"
    )

    # --- append-only, as a database fact ---------------------------------------------------
    #
    # Its own function rather than a reuse of audit_events_append_only(): that one names the
    # table it guards in its message, and a shared function would report the wrong table. Two
    # short functions are cheaper to read than one with a branch.
    op.execute(
        """
        CREATE FUNCTION audit_events_archive_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'audit_events_archive is append-only: % is not permitted', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER trg_audit_events_archive_append_only "
        "BEFORE UPDATE OR DELETE ON audit_events_archive "
        "FOR EACH ROW EXECUTE FUNCTION audit_events_archive_append_only()"
    )


def downgrade() -> None:
    # Reversible, and safely so: DROP TABLE is neither an UPDATE nor a DELETE, so the
    # append-only trigger does not fire and cannot make this migration one-way. The trigger
    # goes with the table; the function does not, so it is dropped explicitly.
    #
    # Downgrading discards the archive. That is the honest consequence and it is acceptable
    # here for one specific reason: nothing is ever removed from ``audit_events``, so every
    # archived event still exists in the active table and re-running the job rebuilds the
    # archive exactly. The day physical deletion is introduced, this downgrade stops being
    # safe and that stage must say so.
    op.execute("DROP TABLE IF EXISTS audit_events_archive")
    op.execute("DROP FUNCTION IF EXISTS audit_events_archive_append_only()")
