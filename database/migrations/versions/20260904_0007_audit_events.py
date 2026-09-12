"""add the audit_events table

Strictly additive, and authorised for exactly this change: **one new table**, its indexes, and
one trigger that guards it. Nothing on ``hotels``, ``users``, ``bookings``, ``payments``,
``user_hotels`` or any other table is altered, and migrations 0001-0006 are untouched.

Stage 4.5.11 recorded the absence of an audit trail as a finding: the platform could move a
booking's dates, rooms and prices, refund a charge, or change who administers a property, and
afterwards nothing said who had done it. This is that record.

**Append-only, enforced by the database.** ``trg_audit_events_append_only`` raises on any
UPDATE or DELETE of a row in this table. The application offers no way to do either -- there
is no service method and no endpoint -- but an audit trail whose immutability lives only in
Python is one that a fixture script, a migration or a psql session can quietly rewrite, and
the whole value of the table is that it cannot be. The trigger is a ROW trigger, so:

* ``TRUNCATE`` still works -- it fires statement-level TRUNCATE triggers, of which there are
  none here. That is what keeps the integration suite's teardown working, and it is the right
  distinction: truncating is administration of the table, not editing of its history.
* ``DROP TABLE`` still works, so ``downgrade()`` below is not fighting its own upgrade.

**No ``updated_at`` and no ``set_updated_at`` trigger**, unlike every other table in this
schema. A row that can never be updated has no moment of last update. ``occurred_at`` is the
only time recorded, and it is defaulted by the database so no caller can influence it.

**``hotel_id`` is nullable, and that is a feature.** A password change and a global catalogue
edit belong to no property; attributing them to one would be an invention, and a NOT NULL
column would force exactly that invention.

**Both foreign keys are ``ON DELETE RESTRICT``**, deliberately unlike ``user_hotels`` and
``platform_admins``, which CASCADE from ``users``. Those two are access-control metadata and
are meaningless once the account is gone; an audit row is the opposite, and RESTRICT is the
policy this schema already uses to protect historical records -- see ``bookings -> hotels``
and ``payments -> bookings``.

CASCADE and SET NULL are not merely undesirable here, they are **unavailable**: both are
implemented as a DELETE or an UPDATE on this table, which the append-only trigger refuses. The
immutability guarantee and the delete policy are therefore the same decision, and RESTRICT is
the only one consistent with it.

The consequence is stated rather than hidden: a property with audit history cannot be deleted,
and neither can an account that has acted. Both compose with what the schema already does --
any property with bookings, guests, rooms, revenue or expenses is already undeletable, and no
endpoint deletes a user at all -- so in practice this refuses one new thing: erasing a hotel
whose only remaining trace is the record of what was done to it.

**Four indexes, one per access pattern**, and no more. Both listing indexes carry
``occurred_at DESC, id DESC`` because the listing is newest-first and needs a total order:
two events written by one transaction can share ``occurred_at`` to the microsecond, and
without the tie-break page 2 could repeat a row from page 1.

Revision ID: 0007_audit_events
Revises: 0006_users_password_changed_at
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_audit_events"
down_revision: str | None = "0006_users_password_changed_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE audit_events (
            id BIGINT GENERATED ALWAYS AS IDENTITY NOT NULL,
            public_id UUID DEFAULT gen_random_uuid() NOT NULL,
            hotel_id BIGINT,
            actor_user_id BIGINT,
            action TEXT NOT NULL,
            resource_type TEXT NOT NULL,
            resource_reference TEXT NOT NULL,
            request_id TEXT,
            details JSONB DEFAULT '{}'::jsonb NOT NULL,
            occurred_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_audit_events PRIMARY KEY (id),
            CONSTRAINT uq_audit_events_public_id UNIQUE (public_id),
            CONSTRAINT fk_audit_events_hotel_id_hotels
                FOREIGN KEY (hotel_id) REFERENCES hotels (id) ON DELETE RESTRICT,
            CONSTRAINT fk_audit_events_actor_user_id_users
                FOREIGN KEY (actor_user_id) REFERENCES users (id) ON DELETE RESTRICT,
            CONSTRAINT ck_audit_events_action_valid CHECK (action = ANY (ARRAY[
                'booking.created',
                'booking.status_changed',
                'booking.stay_modified',
                'payment.created',
                'payment.refund_created',
                'membership.created',
                'membership.role_changed',
                'membership.removed',
                'auth.password_changed',
                'amenity.created',
                'amenity.updated',
                'amenity.deleted',
                'revenue_category.created',
                'revenue_category.updated',
                'revenue_category.deleted',
                'expense_category.created',
                'expense_category.updated',
                'expense_category.deleted'
            ])),
            CONSTRAINT ck_audit_events_resource_type_valid CHECK (resource_type = ANY (ARRAY[
                'booking',
                'payment',
                'membership',
                'user',
                'amenity',
                'revenue_category',
                'expense_category'
            ])),
            CONSTRAINT ck_audit_events_resource_reference_bounded
                CHECK (char_length(resource_reference) BETWEEN 1 AND 64),
            CONSTRAINT ck_audit_events_request_id_shape
                CHECK (request_id IS NULL OR request_id ~ '^[A-Za-z0-9._-]{1,64}$'),
            CONSTRAINT ck_audit_events_details_is_object
                CHECK (jsonb_typeof(details) = 'object')
        )
        """
    )

    # The listing: one hotel, newest first, with a total order so pagination is stable.
    op.execute(
        "CREATE INDEX ix_audit_events_hotel_id_occurred_at "
        "ON audit_events (hotel_id, occurred_at DESC, id DESC)"
    )
    # "What has this person been doing?"
    op.execute(
        "CREATE INDEX ix_audit_events_actor_user_id_occurred_at "
        "ON audit_events (actor_user_id, occurred_at DESC, id DESC)"
    )
    # "What happened to this booking?" -- type first: a reference is unique only within a kind.
    op.execute(
        "CREATE INDEX ix_audit_events_resource_type_resource_reference "
        "ON audit_events (resource_type, resource_reference)"
    )
    # "What else did that one request do?"
    op.execute("CREATE INDEX ix_audit_events_request_id ON audit_events (request_id)")

    # --- append-only, as a database fact ---------------------------------------------------
    #
    # A dedicated function rather than a reuse of set_updated_at: that one exists to WRITE on
    # UPDATE, and this one exists to make UPDATE impossible. Naming it for what it guards
    # keeps the two from ever being confused for one another.
    op.execute(
        """
        CREATE FUNCTION audit_events_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'audit_events is append-only: % is not permitted', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER trg_audit_events_append_only "
        "BEFORE UPDATE OR DELETE ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION audit_events_append_only()"
    )


def downgrade() -> None:
    # The trigger goes with the table; the function does not, so it is dropped explicitly.
    # DROP TABLE is not an UPDATE or a DELETE, so the append-only trigger does not fire and
    # cannot make this migration irreversible.
    op.execute("DROP TABLE IF EXISTS audit_events")
    op.execute("DROP FUNCTION IF EXISTS audit_events_append_only()")
