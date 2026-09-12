"""The audit trail: what happened, who did it, and which request caused it.

Stage 4.5.12. One table, written by the services that perform the mutations, inside the same
transaction as the mutation itself.

**Append-only, and the database is what says so.** There is no update method, no delete
method, no endpoint for either -- and, because a rule that exists only in Python is a rule a
fixture script or a psql session can walk around, a trigger installed by migration 0007
raises on any UPDATE or DELETE against this table. TRUNCATE and DROP still work, so the test
suite's teardown and ``alembic downgrade`` are unaffected: those are administration of the
table, not editing of its history.

**No ``updated_at``, and no ``set_updated_at`` trigger.** Every other table in this schema has
both, and their absence here is the design rather than an omission: a row that can never be
updated has no moment of last update, and a column maintained by a trigger for an event that
must not happen would be machinery arguing with the one above it. :attr:`AuditEvent.occurred_at`
is the only time this table records.

**Both foreign keys are nullable, and they differ on purpose.**

* ``hotel_id`` is null for an event that genuinely belongs to no property -- a password
  change, a global catalogue edit. Inventing a hotel for those would attribute a
  platform-wide action to one tenant, which is worse than saying nothing.
* ``actor_user_id`` is null only for an event no authenticated user caused. Every action
  audited today is performed by an authenticated caller, so today it is always set.

Both are ``ON DELETE RESTRICT``, and that is the same decision as the append-only trigger
rather than a separate one: CASCADE is a DELETE on this table and SET NULL is an UPDATE, so
the trigger refuses both. RESTRICT is the only policy compatible with immutability, and it is
the policy this schema already uses for historical records (``bookings -> hotels``,
``payments -> bookings``). A property with audit history therefore cannot be deleted, and
neither can an account that has acted -- which is what "evidence" means.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, pk_column
from app.models.enums import AuditAction, AuditResourceType

#: The shape a stored request id may take, mirroring
#: :data:`app.core.request_id.VALID_REQUEST_ID` exactly.
#:
#: The middleware already sanitises the header, so a value that reaches this column has been
#: through that filter. The CHECK is the second lock on the same door: this column is read
#: back into an API response, and the id is the one field of an audit row that began life as
#: client-controlled bytes.
REQUEST_ID_SQL_PATTERN = r"^[A-Za-z0-9._-]{1,64}$"


class AuditEvent(Base):
    """One thing that happened, recorded when it committed."""

    __tablename__ = "audit_events"

    id: Mapped[int] = pk_column()
    #: How an event is named outside the database. Audit rows are not addressed individually
    #: by any endpoint, but a listing that identified its rows by nothing would give an
    #: operator no way to quote one in an incident report -- and a BIGINT is not an option.
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()")
    )

    #: The tenant, or NULL for a platform-wide action. See the module docstring.
    hotel_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("hotels.id", ondelete="RESTRICT")
    )
    #: WHO. The authenticated caller, resolved from the bearer token by the existing
    #: dependency chain -- this table stores no credential of any kind.
    actor_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT")
    )

    #: WHAT, from the closed :class:`~app.models.enums.AuditAction` vocabulary.
    action: Mapped[str] = mapped_column(Text, nullable=False)
    #: WHICH KIND of thing, from :class:`~app.models.enums.AuditResourceType`.
    resource_type: Mapped[str] = mapped_column(Text, nullable=False)
    #: WHICH ONE. A public UUID rendered as text, or a natural key such as an amenity code.
    #: TEXT rather than UUID because the global catalogues are addressed by code and have no
    #: UUID at all; bounded by a CHECK so it can never become a free-text dumping ground.
    resource_reference: Mapped[str] = mapped_column(Text, nullable=False)

    #: WHICH REQUEST, so a row here and the lines in the log can be read together. Null for
    #: anything recorded outside an HTTP request.
    request_id: Mapped[str | None] = mapped_column(Text)

    #: WHAT CHANGED, as a small JSONB object whose keys are confined to
    #: :data:`~app.models.enums.SAFE_AUDIT_DETAIL_KEYS`. Never a request body.
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    #: WHEN. Defaulted by the database rather than by the application, so the time recorded is
    #: the server's and no caller can influence it.
    occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("public_id", name="uq_audit_events_public_id"),
        CheckConstraint(f"action IN ({AuditAction.sql_in_list()})", name="action_valid"),
        CheckConstraint(
            f"resource_type IN ({AuditResourceType.sql_in_list()})", name="resource_type_valid"
        ),
        CheckConstraint(
            "char_length(resource_reference) BETWEEN 1 AND 64",
            name="resource_reference_bounded",
        ),
        CheckConstraint(
            f"request_id IS NULL OR request_id ~ '{REQUEST_ID_SQL_PATTERN}'",
            name="request_id_shape",
        ),
        # jsonb_typeof pins the payload to an OBJECT. Without it the column would happily
        # accept a bare string or an array, and every reader would have to defend itself.
        CheckConstraint("jsonb_typeof(details) = 'object'", name="details_is_object"),
        # --- the four access patterns, and only those --------------------------------------
        #
        # Newest-first within one hotel is the listing. `id` breaks ties, because occurred_at
        # is a timestamp and two events in one transaction can share it to the microsecond --
        # without it, page 2 could repeat a row from page 1.
        Index(
            "ix_audit_events_hotel_id_occurred_at",
            "hotel_id",
            text("occurred_at DESC"),
            text("id DESC"),
        ),
        # "What has this person been doing?", ordered the same way.
        Index(
            "ix_audit_events_actor_user_id_occurred_at",
            "actor_user_id",
            text("occurred_at DESC"),
            text("id DESC"),
        ),
        # "What happened to this booking?" -- type first, because a reference is only unique
        # within its kind.
        Index(
            "ix_audit_events_resource_type_resource_reference",
            "resource_type",
            "resource_reference",
        ),
        # "What else did that one request do?"
        Index("ix_audit_events_request_id", "request_id"),
    )


class AuditEventArchive(Base):
    """An audit event copied out of the active table (Stage 4.5.14).

    **A faithful copy, not a summary.** Every column of :class:`AuditEvent` is preserved,
    including the original ``id`` -- which is this table's primary key rather than a new
    surrogate. That single decision buys three things at once: the original identity survives,
    a second archival of the same event is refused by the database rather than by a Python
    check, and two concurrent archival workers cannot produce a duplicate row.

    **The original event is NOT deleted.** ``audit_events`` is append-only at the database
    level -- migration 0007's trigger refuses UPDATE and DELETE -- and Stage 4.5.14 was
    explicitly told not to weaken that guarantee in order to implement retention. So this
    table is an *additive* archive: an event that has been archived exists in both places, and
    the archive row is the record that it was archived. See :mod:`app.services.retention` for
    the full reasoning and what a future deletion stage would need.

    **This table is immutable too.** ``trg_audit_events_archive_append_only`` refuses UPDATE
    and DELETE, exactly as the active table's trigger does. An archive a process can rewrite
    is not an archive.

    **No foreign keys, deliberately** -- and this is the one place the archive differs from
    its source. ``audit_events.hotel_id`` and ``actor_user_id`` are ``ON DELETE RESTRICT``,
    which is right for the active table: it stops a property or an account being erased while
    the record of what it did is still live. Repeating those constraints here would add
    nothing today (the source row still exists, so the RESTRICT still applies) and would work
    against the archive's purpose the day physical deletion arrives: an archive that cannot
    outlive the operational rows it describes is not forensic evidence. The internal keys are
    kept as plain values, and ``hotel_public_id``/``actor_public_id`` are denormalised beside
    them so a row stays meaningful without any join at all.
    """

    __tablename__ = "audit_events_archive"

    #: The ORIGINAL ``audit_events.id``. Not generated, not a surrogate -- the identity is the
    #: point, and making it the primary key is what makes idempotency a database guarantee.
    audit_event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    #: The original ``public_id``, unique here as it is there. A second, independent guard on
    #: the same invariant: two rows claiming to be the same event are refused twice over.
    public_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    #: The original tenant key, NULL for a platform-scoped event -- carried through unchanged,
    #: so the archive draws the same hotel/platform line the active table does.
    hotel_id: Mapped[int | None] = mapped_column(BigInteger)
    #: Denormalised at archival time, so the tenant is identifiable without joining ``hotels``.
    hotel_public_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    actor_user_id: Mapped[int | None] = mapped_column(BigInteger)
    #: Denormalised for the same reason. The actor's EMAIL is deliberately not copied: the
    #: active table does not store it either -- the read APIs join for it -- and an archive
    #: nobody currently reads is the wrong place to start keeping personal data at rest.
    actor_public_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    action: Mapped[str] = mapped_column(Text, nullable=False)
    resource_type: Mapped[str] = mapped_column(Text, nullable=False)
    resource_reference: Mapped[str] = mapped_column(Text, nullable=False)
    request_id: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    #: The original occurrence time, copied verbatim. This is what the retention cutoff is
    #: judged against, and copying rather than re-deriving it is why an archived event still
    #: says when it happened rather than when it was filed.
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    #: When the copy was made. The only column that is not in the source, and the only one the
    #: archive adds: it answers "when did this leave the active window?", which the source row
    #: cannot.
    archived_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("public_id", name="uq_audit_events_archive_public_id"),
        # The action and resource vocabularies are NOT re-CHECKed here. The active table
        # already refused anything outside them on the way in, and a second copy of the list
        # would be a second thing to keep in step with the enum -- one that could reject a row
        # this application had already accepted, turning a retention job into an outage.
        #
        # The one access pattern an archive actually has: "what do we hold from this period?".
        # Ordered like the active listing so the two read the same way.
        Index(
            "ix_audit_events_archive_occurred_at",
            text("occurred_at DESC"),
            text("audit_event_id DESC"),
        ),
    )


__all__ = ["REQUEST_ID_SQL_PATTERN", "AuditEvent", "AuditEventArchive"]
