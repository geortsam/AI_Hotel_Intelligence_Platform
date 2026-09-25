"""create hotel_documents and hotel_document_chunks; widen the audit vocabulary for them

Authorised in Stage 7.9 for exactly this, and nothing else:

1. **Two new tables**, `hotel_documents` (one row per document version) and
   `hotel_document_chunks` (one row per retrievable chunk), with a GIN index on the chunks'
   full-text search column -- architecture §6.2 and the Stage 7.9 roadmap entry.
2. **Three triggers.** The shared `set_updated_at` on `hotel_documents`; a guard on
   `hotel_documents` that refuses DELETE and refuses any UPDATE except the two permitted status
   transitions; and an append-only guard on `hotel_document_chunks`.
3. **The audit vocabulary widened** by three actions (`document.created`,
   `document.version_created`, `document.withdrawn`) and one resource type (`document`), exactly
   as 0009 and 0012 widened it. `trg_audit_events_append_only` is not touched.

No existing table, column, index or trigger is altered beyond the two audit CHECKs, and
migrations 0001-0013 are unchanged.

**Versioning is by new row.** A version is immutable: the guard trigger permits only
`active -> superseded` and `active -> withdrawn`, touching nothing but `status` and `updated_at`.
**Withdrawal keeps the chunks** (Stage 7.9 decision): retrieval excludes non-active versions in its
WHERE clause, and every chunk keeps its `public_id` so a past citation still resolves.

**Tenancy is structural.** `hotel_id` is on the document, `ON DELETE RESTRICT`; a version can
supersede only a version of the same hotel, through a composite foreign key over
`(supersedes_id, hotel_id)`.

**The downgrade is conditionally safe**, like 0009's and 0012's: it succeeds only while no audit
event uses the new vocabulary, because `audit_events` is append-only.

Revision ID: 0014_hotel_documents
Revises: 0013_llm_invocations
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014_hotel_documents"
down_revision: str | None = "0013_llm_invocations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The twenty actions 0007, 0009 and 0012 established, verbatim.
_PREVIOUS_ACTIONS = (
    "booking.created",
    "booking.status_changed",
    "booking.stay_modified",
    "payment.created",
    "payment.refund_created",
    "membership.created",
    "membership.role_changed",
    "membership.removed",
    "auth.password_changed",
    "amenity.created",
    "amenity.updated",
    "amenity.deleted",
    "revenue_category.created",
    "revenue_category.updated",
    "revenue_category.deleted",
    "expense_category.created",
    "expense_category.updated",
    "expense_category.deleted",
    "booking.deleted",
    "tool.invoked",
)
_ADDED_ACTIONS = ("document.created", "document.version_created", "document.withdrawn")

#: The eight resource types 0007 and 0012 established.
_PREVIOUS_RESOURCE_TYPES = (
    "booking",
    "payment",
    "membership",
    "user",
    "amenity",
    "revenue_category",
    "expense_category",
    "tool",
)
_ADDED_RESOURCE_TYPE = "document"


def _vocabulary(constraint: str, column: str, values: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{value}'" for value in values)
    return (
        f"ALTER TABLE audit_events ADD CONSTRAINT {constraint} "
        f"CHECK ({column} = ANY (ARRAY[{listed}]))"
    )


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE hotel_documents (
            id BIGINT GENERATED ALWAYS AS IDENTITY NOT NULL,
            public_id UUID DEFAULT gen_random_uuid() NOT NULL,
            hotel_id BIGINT NOT NULL,
            title TEXT NOT NULL,
            source TEXT,
            language TEXT NOT NULL,
            content_checksum TEXT NOT NULL,
            version INTEGER NOT NULL,
            supersedes_id BIGINT,
            status TEXT NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_hotel_documents PRIMARY KEY (id),
            CONSTRAINT uq_hotel_documents_public_id UNIQUE (public_id),
            CONSTRAINT uq_hotel_documents_id_hotel_id UNIQUE (id, hotel_id),
            CONSTRAINT uq_hotel_documents_supersedes_id UNIQUE (supersedes_id),
            CONSTRAINT fk_hotel_documents_hotel_id_hotels
                FOREIGN KEY (hotel_id) REFERENCES hotels (id) ON DELETE RESTRICT,
            CONSTRAINT fk_hotel_documents_supersedes_id_hotel_id_hotel_documents
                FOREIGN KEY (supersedes_id, hotel_id)
                REFERENCES hotel_documents (id, hotel_id) ON DELETE RESTRICT,
            CONSTRAINT ck_hotel_documents_title_bounded
                CHECK (char_length(title) BETWEEN 1 AND 200),
            CONSTRAINT ck_hotel_documents_source_bounded
                CHECK (source IS NULL OR char_length(source) BETWEEN 1 AND 200),
            CONSTRAINT ck_hotel_documents_language_valid CHECK (language IN (
                'simple', 'english', 'greek', 'french', 'german', 'italian', 'spanish'
            )),
            CONSTRAINT ck_hotel_documents_content_checksum_shape
                CHECK (content_checksum ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_hotel_documents_version_positive CHECK (version >= 1),
            CONSTRAINT ck_hotel_documents_first_version_supersedes_nothing
                CHECK ((version = 1) = (supersedes_id IS NULL)),
            CONSTRAINT ck_hotel_documents_status_valid
                CHECK (status IN ('active', 'superseded', 'withdrawn'))
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_hotel_documents_hotel_id_status ON hotel_documents (hotel_id, status)"
    )
    op.execute(
        "CREATE TRIGGER trg_hotel_documents_set_updated_at BEFORE UPDATE ON hotel_documents "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    # A version is immutable. The only permitted UPDATE moves an ACTIVE version to superseded or
    # withdrawn, and changes nothing else; DELETE is refused (hard deletion is a separate, audited
    # operation no stage has built). Named for what it guards, like audit_events_append_only.
    op.execute(
        """
        CREATE FUNCTION hotel_documents_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'hotel_documents versions are never deleted'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF (NEW.id, NEW.public_id, NEW.hotel_id, NEW.title, NEW.source, NEW.language,
                NEW.content_checksum, NEW.version, NEW.supersedes_id, NEW.created_at)
               IS DISTINCT FROM
               (OLD.id, OLD.public_id, OLD.hotel_id, OLD.title, OLD.source, OLD.language,
                OLD.content_checksum, OLD.version, OLD.supersedes_id, OLD.created_at) THEN
                RAISE EXCEPTION 'a hotel_documents version is immutable; upload a new version'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status
               AND NOT (OLD.status = 'active' AND NEW.status IN ('superseded', 'withdrawn')) THEN
                RAISE EXCEPTION 'hotel_documents status % -> % is not permitted',
                    OLD.status, NEW.status
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER trg_hotel_documents_guard BEFORE UPDATE OR DELETE ON hotel_documents "
        "FOR EACH ROW EXECUTE FUNCTION hotel_documents_guard()"
    )

    op.execute(
        """
        CREATE TABLE hotel_document_chunks (
            id BIGINT GENERATED ALWAYS AS IDENTITY NOT NULL,
            public_id UUID DEFAULT gen_random_uuid() NOT NULL,
            document_id BIGINT NOT NULL,
            ordinal INTEGER NOT NULL,
            text TEXT NOT NULL,
            token_count INTEGER NOT NULL,
            search_vector TSVECTOR NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_hotel_document_chunks PRIMARY KEY (id),
            CONSTRAINT uq_hotel_document_chunks_public_id UNIQUE (public_id),
            CONSTRAINT uq_hotel_document_chunks_document_id_ordinal UNIQUE (document_id, ordinal),
            CONSTRAINT fk_hotel_document_chunks_document_id_hotel_documents
                FOREIGN KEY (document_id) REFERENCES hotel_documents (id) ON DELETE RESTRICT,
            CONSTRAINT ck_hotel_document_chunks_ordinal_non_negative CHECK (ordinal >= 0),
            CONSTRAINT ck_hotel_document_chunks_text_bounded
                CHECK (char_length(text) BETWEEN 1 AND 4000),
            CONSTRAINT ck_hotel_document_chunks_token_count_positive CHECK (token_count >= 1)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_hotel_document_chunks_search_vector "
        "ON hotel_document_chunks USING gin (search_vector)"
    )
    op.execute(
        """
        CREATE FUNCTION hotel_document_chunks_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'hotel_document_chunks is append-only: % is not permitted', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER trg_hotel_document_chunks_append_only "
        "BEFORE UPDATE OR DELETE ON hotel_document_chunks "
        "FOR EACH ROW EXECUTE FUNCTION hotel_document_chunks_append_only()"
    )

    # --- the audit vocabulary, widened as 0009 and 0012 widened it ------------------------------
    op.execute("ALTER TABLE audit_events DROP CONSTRAINT ck_audit_events_action_valid")
    op.execute(
        _vocabulary(
            "ck_audit_events_action_valid", "action", (*_PREVIOUS_ACTIONS, *_ADDED_ACTIONS)
        )
    )
    op.execute("ALTER TABLE audit_events DROP CONSTRAINT ck_audit_events_resource_type_valid")
    op.execute(
        _vocabulary(
            "ck_audit_events_resource_type_valid",
            "resource_type",
            (*_PREVIOUS_RESOURCE_TYPES, _ADDED_RESOURCE_TYPE),
        )
    )


def downgrade() -> None:
    # Succeeds only while no audit event uses the added vocabulary; see the module docstring.
    op.execute("ALTER TABLE audit_events DROP CONSTRAINT ck_audit_events_resource_type_valid")
    op.execute(
        _vocabulary(
            "ck_audit_events_resource_type_valid", "resource_type", _PREVIOUS_RESOURCE_TYPES
        )
    )
    op.execute("ALTER TABLE audit_events DROP CONSTRAINT ck_audit_events_action_valid")
    op.execute(_vocabulary("ck_audit_events_action_valid", "action", _PREVIOUS_ACTIONS))

    # Tables go with their triggers; the two functions are dropped explicitly. DROP TABLE is
    # neither an UPDATE nor a DELETE, so the guards cannot make this irreversible.
    op.execute("DROP TABLE IF EXISTS hotel_document_chunks")
    op.execute("DROP TABLE IF EXISTS hotel_documents")
    op.execute("DROP FUNCTION IF EXISTS hotel_document_chunks_append_only()")
    op.execute("DROP FUNCTION IF EXISTS hotel_documents_guard()")
