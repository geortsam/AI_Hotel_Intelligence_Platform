"""give llm_invocations a retention period: expired rows may be deleted, and nothing else

Authorised for exactly this: **one function body replaced**. No table, column, constraint, index
or trigger is created, altered or dropped. ``trg_llm_invocations_append_only`` still fires BEFORE
UPDATE OR DELETE on every row and still calls ``llm_invocations_append_only()``; only what that
function permits changes.

**The contract (Issue 4, approved).** An invocation record is kept for
``llm_invocation_retention_days`` -- 365 by default, a day being exactly 24 hours -- after its
``created_at``, and is then physically deleted by the operator's purge
(``python -m app.jobs.purge_llm_invocations``). Migration 0013 deferred this decision and recorded
it as a known limitation; this revision is the decision.

**What the database now permits, and what it still refuses.**

* UPDATE: refused, always. A record still never changes.
* DELETE: refused, unless the transaction has declared a retention period with
  ``set_config('app.llm_invocation_retention_hours', '<hours>', true)`` -- transaction-local, so
  it cannot leak into a later transaction on a pooled connection -- AND the row is at least that
  old: ``created_at <= now() - <hours>``. The purge declares the period it was configured with;
  the database checks every row against it.
* A declared period shorter than 24 hours is refused outright, whatever the row: no setting, bug
  or psql session can use this path to remove a record younger than a day.
* TRUNCATE: unchanged -- neither an UPDATE nor a DELETE, so the disposable test databases can
  still be reset.

**Why this is not the audit trail's refusal reversed.** ``app.services.retention`` refuses to
delete from ``audit_events`` because an exemption would make the audit trail rewritable. That
refusal stands and this revision does not touch ``audit_events``. An invocation record is a cost
and attribution account, not evidence of a business change, and it now has a retention period
its owner chose; the exemption here deletes only what that period has already expired, and no
path updates anything.

``ON DELETE RESTRICT`` to ``hotels`` and ``users`` is unchanged: a hotel or user with an unexpired
record still cannot be deleted. Once the purge has removed a hotel's last record, this table no
longer blocks its deletion (``audit_events`` still may).

Revision ID: 0017_llm_invocation_retention
Revises: 0016_demand_observation_periods
Create Date: 2026-10-03
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017_llm_invocation_retention"
down_revision: str | None = "0016_demand_observation_periods"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION llm_invocations_append_only() RETURNS trigger AS $$
        DECLARE
            declared TEXT;
            retention_hours INTEGER;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                declared := current_setting('app.llm_invocation_retention_hours', true);
                IF declared IS NOT NULL AND declared <> '' THEN
                    retention_hours := declared::INTEGER;
                    IF retention_hours < 24 THEN
                        RAISE EXCEPTION
                            'llm_invocations retention must be at least 24 hours, got %',
                            retention_hours
                            USING ERRCODE = 'restrict_violation';
                    END IF;
                    IF OLD.created_at <= now() - make_interval(hours => retention_hours) THEN
                        RETURN OLD;
                    END IF;
                    RAISE EXCEPTION
                        'llm_invocations: DELETE of a record inside its retention period '
                        'is not permitted'
                        USING ERRCODE = 'restrict_violation';
                END IF;
            END IF;
            RAISE EXCEPTION
                'llm_invocations is append-only: % is not permitted', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )


def downgrade() -> None:
    # Migration 0013's body, restored verbatim. Records the purge already deleted stay deleted:
    # a downgrade restores the rule, not the rows.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION llm_invocations_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'llm_invocations is append-only: % is not permitted', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
