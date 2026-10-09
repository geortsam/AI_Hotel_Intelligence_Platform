"""give a guest's email address a shape, and make its uniqueness case-insensitive

Authorised for exactly this: **one unique index replaced by another, and one CHECK added**, both
on ``guests``. No table or column is created, altered or dropped, and no row is written -- in
particular no address is lower-cased, trimmed or otherwise rewritten.

**The defect (Issue H7).** Migration 0001 declared

    uq_guests_hotel_id_email ON guests (hotel_id, email) WHERE email IS NOT NULL

which compares the text exactly, so ``Elena@x.test`` and ``elena@x.test`` could both be stored
for one hotel: two guest records for one mailbox. And nothing gave the column a shape --
``not-an-email`` was stored as written -- although ``users.email`` has carried
``ck_users_email_format`` since 0003.

**The correction.**

    uq_guests_hotel_id_lower_email ON guests (hotel_id, lower(email)) WHERE email IS NOT NULL
    ck_guests_email_format CHECK (email IS NULL OR email ~ '^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$')

The pattern is ``ck_users_email_format``'s, character for character. The API lower-cases and
checks the same rule before anything reaches the table; these make the database hold it on its
own, for any writer. A guest with no address is still unconstrained by either, and any number
of them may share a hotel.

**Existing rows are checked, never repaired.** Before anything is built, ``upgrade`` looks for
addresses at one hotel that differ only by letter case, and for addresses the CHECK would
refuse. If it finds either it stops and changes nothing, naming each hotel by its slug and each
offending group by its guests' public identifiers -- never by the address itself, which is
personal data and would land in a deployment log. Resolving them is a human decision (which
record is the real guest, what the address should have been), so it is left to one.

**Downgrade** drops the CHECK and restores 0001's case-sensitive index exactly. It cannot fail
on data: rows that satisfy the case-insensitive key satisfy the case-sensitive one.

Revision ID: 0020_guest_email_rules
Revises: 0019_review_external_id_scope
Create Date: 2026-10-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_guest_email_rules"
down_revision: str | None = "0019_review_external_id_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The partial predicate both keys share: a guest without an address is not constrained.
PREDICATE = "email IS NOT NULL"

#: 0001's key, compared exactly, and the key that replaces it.
CASE_SENSITIVE_INDEX = "uq_guests_hotel_id_email"
CASE_INSENSITIVE_INDEX = "uq_guests_hotel_id_lower_email"

FORMAT_CHECK = "ck_guests_email_format"
#: ``ck_users_email_format``'s pattern (0003), character for character.
EMAIL_FORMAT = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

#: How many offending groups a refusal lists by name. The count is always complete.
SHOWN = 20


def _groups(sql: str, **params: object) -> list[tuple[str, list[str]]]:
    """``(hotel slug, [guest public ids])`` for each offending group, identified by nothing
    personal."""
    rows = op.get_bind().execute(sa.text(sql), params).all()
    return [(str(slug), [str(guest) for guest in guests]) for slug, guests in rows]


def _case_duplicates() -> list[tuple[str, list[str]]]:
    """Addresses at one hotel that differ only by letter case: one group per address."""
    return _groups(
        "SELECT h.slug, array_agg(g.public_id::text ORDER BY g.id) "
        "FROM guests g JOIN hotels h ON h.id = g.hotel_id "
        f"WHERE g.{PREDICATE} "
        "GROUP BY h.slug, g.hotel_id, lower(g.email) HAVING count(*) > 1 "
        "ORDER BY h.slug, min(g.id)"
    )


def _malformed() -> list[tuple[str, list[str]]]:
    """Addresses the CHECK would refuse: one group per hotel."""
    return _groups(
        "SELECT h.slug, array_agg(g.public_id::text ORDER BY g.id) "
        "FROM guests g JOIN hotels h ON h.id = g.hotel_id "
        f"WHERE g.{PREDICATE} AND g.email !~ :pattern "
        "GROUP BY h.slug ORDER BY h.slug",
        pattern=EMAIL_FORMAT,
    )


def _describe(groups: list[tuple[str, list[str]]]) -> str:
    shown = "; ".join(f"hotel {slug!r}: guests {', '.join(ids)}" for slug, ids in groups[:SHOWN])
    hidden = len(groups) - SHOWN
    return shown + (f"; and {hidden} more" if hidden > 0 else "")


def upgrade() -> None:
    duplicates = _case_duplicates()
    malformed = _malformed()
    if duplicates or malformed:
        problems = []
        if duplicates:
            problems.append(
                f"{len(duplicates)} group(s) of guests at one hotel share an email address "
                f"that differs only by letter case ({_describe(duplicates)})"
            )
        if malformed:
            count = sum(len(ids) for _, ids in malformed)
            problems.append(
                f"{count} guest email address(es) do not match {EMAIL_FORMAT} "
                f"({_describe(malformed)})"
            )
        raise RuntimeError(
            "0020 refused: "
            + "; and ".join(problems)
            + ". No address is shown, and nothing was changed: correct or merge those guests, "
            "then run the upgrade again."
        )
    # The new key is built before the old one goes, so no moment is left unconstrained.
    op.execute(
        f"CREATE UNIQUE INDEX {CASE_INSENSITIVE_INDEX} "
        f"ON guests (hotel_id, lower(email)) WHERE {PREDICATE}"
    )
    op.execute(f"DROP INDEX {CASE_SENSITIVE_INDEX}")
    op.execute(
        f"ALTER TABLE guests ADD CONSTRAINT {FORMAT_CHECK} "
        f"CHECK (email IS NULL OR email ~ '{EMAIL_FORMAT}')"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE guests DROP CONSTRAINT {FORMAT_CHECK}")
    op.execute(
        f"CREATE UNIQUE INDEX {CASE_SENSITIVE_INDEX} ON guests (hotel_id, email) WHERE {PREDICATE}"
    )
    op.execute(f"DROP INDEX {CASE_INSENSITIVE_INDEX}")
