"""The retention of `llm_invocations`: what expires, and the job that deletes it (Issue 4).

## The contract

An invocation record -- one copilot question's content-free account: hotel, actor, prompt
version, provider, model, outcome, tokens, latency, request id -- is kept for
`llm_invocation_retention_days` (365 by default, the period the project owner approved) after
its `created_at`, a day being exactly 24 hours. After that it is **deleted**, physically, by
:func:`purge_expired_invocations`; nothing is archived. Until the purge runs an expired record is
still in the table: no request reads this table, so there is nothing to hide it from. How often
an operator runs the purge bounds how long an expired record outlives its period.

`invocation_public_id` -- the support reference a copilot answer carries -- identifies its record
for exactly as long as the record exists: the retention period, then nothing.

## What the job does, and does not

* **One hotel at a time, in bounded batches.** Every DELETE is scoped to one hotel's id and to
  at most `batch_size` of its oldest expired records, and runs in its own transaction: a failure
  leaves every earlier batch deleted and nothing half-done, and running the job again finishes
  the work. No statement can reach two hotels.
* **The database checks every row.** Each batch declares the retention period to migration
  0017's trigger, which refuses to delete any record younger than it -- and any period shorter
  than a day -- whatever this code computes.
* **No update, ever.** The trigger still refuses every UPDATE.
* **No actor, no audit event, no model, no allowance**, for the reasons `app.services.retention`
  gives for audit archival: the job acts for nobody, and its counts belong in the operator's
  output and logs, not in the tenant-visible audit history. Unlike audit archival it deletes:
  an accounting record is not evidence of a business change, and its owner chose a period.
* **Conversations are independent.** The conversation purge never touches this table and this
  job never touches conversations or the audit trail; the settings validator keeps this period
  at least as long as the conversation retention, so no live turn's `request_id` outlives the
  record it names.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.repositories.llm_invocation import LlmInvocationRepository

logger = logging.getLogger(__name__)

#: The most records one batch deletes: a short transaction on any hardware this runs on, the
#: same bound the audit archival copies with.
PURGE_BATCH = 1_000


@dataclass(frozen=True, slots=True)
class InvocationPurgeResult:
    """What one purge did, in counts only. No identifier, no hotel, no actor."""

    #: Expired invocation records physically deleted.
    invocations_deleted: int
    #: Hotels that held at least one expired record when the purge began.
    hotels: int
    #: Batches run: one transaction each, including each hotel's last, short one.
    batches: int
    #: The retention period the cutoff was computed from, in days of exactly 24 hours.
    retention_days: int


def purge_expired_invocations(
    session: Session, settings: Settings, *, batch_size: int = PURGE_BATCH
) -> InvocationPurgeResult:
    """The job entry point: physically delete every expired invocation record, at every hotel.

    **A function, not an endpoint**, on the precedent of ``purge_expired_conversations`` and
    ``archive_audit_events``: the ``app.jobs.purge_llm_invocations`` command, a scheduler or an
    operator with a Python shell calls it with a session and the application's settings. There
    is no route, so there is no destructive retention API to authorize, rate-limit or expose.

    For each hotel holding an expired record, in id order, batches run until one deletes fewer
    than ``batch_size`` -- nothing expired was left at that hotel when it ran. A record that
    expires while the job runs may be left for the next run. Running it again is safe: a second
    run finds nothing and deletes nothing.
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be at least 1, got {batch_size}")
    retention_days = settings.llm_invocation_retention_days
    repository = LlmInvocationRepository(session)

    try:
        hotel_ids = list(repository.hotels_with_expired(retention_days))
        session.commit()  # the read's transaction ends here; each batch is its own
    except Exception:
        session.rollback()
        raise

    deleted = 0
    batches = 0
    for hotel_id in hotel_ids:
        while True:
            try:
                purged = repository.purge_expired(hotel_id, retention_days, limit=batch_size)
                session.commit()
            except Exception:
                session.rollback()
                raise
            deleted += purged
            batches += 1
            if purged < batch_size:
                break

    logger.info(
        "llm invocation purge: %s deleted at %s hotel(s) in %s batch(es)",
        deleted,
        len(hotel_ids),
        batches,
        extra={
            "llm_invocations_purged": deleted,
            "purge_hotels": len(hotel_ids),
            "purge_batches": batches,
        },
    )
    return InvocationPurgeResult(
        invocations_deleted=deleted,
        hotels=len(hotel_ids),
        batches=batches,
        retention_days=retention_days,
    )


__all__ = ["PURGE_BATCH", "InvocationPurgeResult", "purge_expired_invocations"]
