"""Audit retention: the policy, and the job that applies it.

Stage 4.5.14. One authoritative definition of how long an audit event stays in the active
table, and one bounded, idempotent, concurrency-safe process that copies older events into the
archive.

# The decision this stage had to make, stated plainly

**Nothing is deleted from ``audit_events``, and that is a refusal rather than an omission.**

Migration 0007 installed ``trg_audit_events_append_only``: any UPDATE or DELETE against the
audit table raises. That trigger is the reason the audit trail is evidence rather than a log --
it holds against this application, against a fixture script, and against somebody with a psql
prompt. Physically removing archived rows would mean dropping it, disabling it for the job's
session, or granting the job an exemption, and each of those is the same thing wearing a
different hat: an audit trail that a sufficiently privileged process can rewrite.

Stage 4.5.14's brief anticipated exactly this and said so: if physical removal conflicts with
the immutability model, implement the archival copy, the verification and the eligibility
tracking, and **do not** weaken the guarantee to get the deletion. So:

* eligible events are copied into :class:`~app.models.audit.AuditEventArchive`;
* the copy is verified field by field before the transaction commits;
* the archive row IS the eligibility record -- "has this event been archived?" is answered by
  a primary-key lookup, not by a flag on the source row (which could not be written anyway,
  since writing it would be an UPDATE);
* the active table keeps every row it ever had.

What this buys today is a durable, immutable, independently meaningful second copy, and an
exact answer to "what is past retention and has it been secured?". What it does not buy is
smaller storage. That is the honest trade, and reversing it is a separate decision with its own
migration -- one that would have to explain how it keeps the append-only guarantee while
removing rows, and which would need this archive to exist first regardless.

# Why the job emits no audit event of its own

The obvious symmetry -- "archival is a significant operation, so audit it" -- is refused here
for two reasons that compound.

*The actor model.* :class:`~app.services.audit.AuditTrail` binds the authenticated caller from
the request context, deliberately, so a service cannot name the wrong identity. The archival
job runs on no request and for no user. Recording it would mean inventing a system actor, which
is a change to the actor model rather than a use of it.

*Recursion.* An ``audit.archived`` event would be written into ``audit_events``, would itself
become eligible under this very policy, and would be archived by a later run -- which, if that
run also recorded itself, would emit another. A retention mechanism whose own bookkeeping is
subject to retention grows the table it exists to bound.

The job returns :class:`ArchivalResult` instead. Operational visibility belongs in the caller's
logs and metrics, which is where an operator looks for it, and not in the tenant-visible audit
history where an operator's scheduled job would be noise between the events people care about.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.repositories.audit_archive import AuditArchiveRepository

logger = logging.getLogger(__name__)


class ArchiveVerificationError(RuntimeError):
    """A batch's archived rows did not match their source.

    Deliberately NOT an :class:`~app.core.errors.AppError`: this cannot reach a client, because
    no endpoint invokes archival. It is an operator-facing failure, and it aborts the batch --
    the transaction is rolled back, so an unverified copy never becomes the state of record.
    """


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """How long audit events stay in the active table, and how they leave it.

    **The single authoritative definition.** Every number the archival job uses comes from
    here, and this is built from :class:`~app.core.config.Settings` -- so a deployment changes
    retention by configuration, and no module re-decides it locally.
    """

    #: Days an event stays out of the archive, counted back from "now".
    retention_days: int
    #: The most events one batch may copy.
    batch_size: int
    #: The most batches one invocation may run, so a large backlog cannot hold a process
    #: indefinitely. The result says whether more work remains.
    max_batches: int

    @classmethod
    def from_settings(cls, settings: Settings) -> RetentionPolicy:
        return cls(
            retention_days=settings.audit_retention_days,
            batch_size=settings.audit_archive_batch_size,
            max_batches=settings.audit_archive_max_batches,
        )

    def cutoff(self, now: dt.datetime) -> dt.datetime:
        """The moment before which an event is eligible for archival.

        Deterministic: the same *now* always yields the same cutoff, which is what lets a test
        assert boundary behaviour exactly and lets two concurrent workers agree on the eligible
        set without coordinating.

        *now* must be timezone-aware. Every timestamp in this schema is ``TIMESTAMPTZ``, and a
        naive datetime compared against one is a silent wrong answer that depends on the
        server's zone -- so it is refused rather than localised on a guess.

        Eligibility is ``occurred_at < cutoff``, strictly. An event occurring exactly on the
        boundary is RETAINED: the active window owns its own edge, so "retained for N days"
        means at least N days rather than almost N.
        """
        if now.tzinfo is None:
            raise ValueError("retention cutoff requires a timezone-aware 'now'")
        return now - dt.timedelta(days=self.retention_days)


@dataclass(frozen=True, slots=True)
class ArchivalResult:
    """What one invocation of the archival job did.

    Counts, times and sizes -- and deliberately nothing else. No event details, no actor, no
    email, no request id, no identifiers of any kind: this is designed to be safe to log
    verbatim, and the way to keep it safe is for it to contain nothing that could be unsafe.
    """

    cutoff: dt.datetime
    batch_size: int
    batches_run: int
    examined: int
    archived: int
    already_archived: int
    remaining: int
    duration_seconds: float

    @property
    def complete(self) -> bool:
        """Whether the backlog was cleared, or the batch ceiling stopped the run first."""
        return self.remaining == 0


class AuditRetentionService:
    """Applies the retention policy. Copies; never deletes.

    Owns its transaction, like every other writing service in this layer: one batch is one
    unit of work, and its copy and its verification commit together or not at all.
    """

    def __init__(
        self,
        session: Session,
        repository: AuditArchiveRepository,
        policy: RetentionPolicy,
    ) -> None:
        self._session = session
        self._repository = repository
        self._policy = policy

    @property
    def policy(self) -> RetentionPolicy:
        return self._policy

    def run(self, *, now: dt.datetime | None = None) -> ArchivalResult:
        """Archive everything past the cutoff, in bounded batches.

        *now* is injectable so a test can place the cutoff exactly rather than sleeping. In
        production it is simply the current UTC time.

        The loop stops on the first of three conditions: nothing left to archive, a batch that
        copied nothing (which means another worker took the remainder), or the configured
        ceiling. It cannot spin: every batch either archives at least one row or breaks.
        """
        started = dt.datetime.now(dt.UTC)
        cutoff = self._policy.cutoff(now or started)

        eligible = self._repository.count_eligible(cutoff)
        pending_before = self._repository.count_pending(cutoff)

        archived = 0
        batches = 0
        for _ in range(self._policy.max_batches):
            copied = self._archive_one_batch(cutoff)
            batches += 1
            archived += copied
            if copied == 0:
                break

        remaining = self._repository.count_pending(cutoff)
        result = ArchivalResult(
            cutoff=cutoff,
            batch_size=self._policy.batch_size,
            batches_run=batches,
            examined=eligible,
            archived=archived,
            # Eligible events that were already in the archive when this run started. Derived
            # rather than counted separately, so it cannot contradict the two figures it sits
            # between.
            already_archived=eligible - pending_before,
            remaining=remaining,
            duration_seconds=(dt.datetime.now(dt.UTC) - started).total_seconds(),
        )

        # Counts, sizes and a duration. Nothing here can carry an event's details, an actor, an
        # address or an internal key, because the result type has nowhere to put one.
        logger.info(
            "Audit archival complete (examined=%d archived=%d already_archived=%d "
            "remaining=%d batches=%d batch_size=%d seconds=%.3f)",
            result.examined,
            result.archived,
            result.already_archived,
            result.remaining,
            result.batches_run,
            result.batch_size,
            result.duration_seconds,
        )
        return result

    def _archive_one_batch(self, cutoff: dt.datetime) -> int:
        """Copy one batch, verify it, and commit. Returns how many rows this call archived.

        The order is the guarantee:

        1. the copy is staged by a single ``INSERT ... SELECT`` in the database;
        2. the rows it actually inserted are verified against their source, field by field,
           still inside the transaction;
        3. only then does anything commit.

        A verification failure rolls the batch back, so an unverified copy is never durable. A
        database failure does the same and is re-raised without its driver message -- the
        SQLSTATE goes to the log and nothing else, as everywhere else in this layer.
        """
        try:
            copied = self._repository.archive_batch(cutoff, limit=self._policy.batch_size)
            if copied:
                self._verify(copied)
            self._session.commit()
        except ArchiveVerificationError:
            self._session.rollback()
            raise
        except SQLAlchemyError as exc:
            self._session.rollback()
            # No exc_info: a failed INSERT against this table renders the audit rows it was
            # copying into the driver message, which is the payload this project has spent
            # eleven stages keeping out of logs.
            logger.error(
                "Audit archival batch failed (sqlstate=%s)",
                getattr(getattr(exc, "orig", None), "sqlstate", None),
            )
            raise
        return len(copied)

    def _verify(self, archived_ids: list[int]) -> None:
        """Refuse to commit a batch whose archived rows do not match their source."""
        faithful = self._repository.count_faithful_copies(archived_ids)
        if faithful != len(archived_ids):
            # The count and the shortfall, never the ids: an operator needs to know a batch
            # failed verification, not which audit events were in it.
            raise ArchiveVerificationError(
                f"{len(archived_ids) - faithful} of {len(archived_ids)} archived audit events "
                "do not match their source; the batch was rolled back."
            )


def archive_audit_events(
    session: Session, settings: Settings, *, now: dt.datetime | None = None
) -> ArchivalResult:
    """The job entry point.

    **A function, not an endpoint.** Stage 4.5.14 asked for a service or job entry point rather
    than an HTTP surface, and this is it: a scheduler, a management command or an operator with
    a Python shell calls this with a session and the application's settings. There is
    deliberately no route, so there is no destructive retention API to authorize, rate-limit or
    accidentally expose to a hotel user -- and no way for any API caller to influence the
    cutoff.

    It is not wired to a scheduler in this stage. Choosing and configuring one is a deployment
    decision, and this stage was scoped to the mechanism.
    """
    service = AuditRetentionService(
        session, AuditArchiveRepository(session), RetentionPolicy.from_settings(settings)
    )
    return service.run(now=now)


__all__ = [
    "ArchivalResult",
    "ArchiveVerificationError",
    "AuditRetentionService",
    "RetentionPolicy",
    "archive_audit_events",
]
