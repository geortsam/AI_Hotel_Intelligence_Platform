"""The migration chain's identity, pinned so that a fresh clone reproduces the same number.

A checksum over the migration files answers one question: *has anyone edited a revision that
has already been applied somewhere?* Alembic will not notice -- it records only the revision
id in `alembic_version` -- so the bytes have to be checked separately.

The obvious implementation, sha256 over the files exactly as they sit on disk, is wrong here,
and quietly so. `.gitattributes` declares `* text=auto`, which means the repository stores LF
and Git hands each checkout whatever its platform wants: LF on the Linux CI runner, CRLF on a
Windows machine with `core.autocrlf=true`. The bytes on disk are therefore a property of *the
checkout*, not of the migrations. A raw hash computed on one machine cannot be reproduced on
another, which makes it useless as the thing it is supposed to be -- a shared constant that
two people can compare.

So the content is canonicalised before hashing, and only for hashing:

1. every ``*.py`` under ``database/migrations/versions`` is collected;
2. ordered by filename ascending -- the ``YYYYMMDD_NNNN_`` prefix makes that the chain order
   as well, and it is a pure byte comparison, so no locale can reorder it;
3. read as **bytes** -- never decoded, so no encoding or locale is involved;
4. every ``\r\n`` is replaced with ``\n``. This is the whole of the canonicalisation, and it
   is applied to the copy being hashed. **No migration file is ever written by this module.**
5. sha256 over the concatenation, in that order, with no separators and no filenames mixed in.

The result is identical on Windows and Linux, before and after a clone, which
``test_the_digest_does_not_depend_on_how_git_checked_the_files_out`` proves rather than
asserts -- it hashes a CRLF rendering and an LF rendering of the same content and requires
both to land on the constant below.

Because filenames are not part of the digest, this pins *content*. The filenames and the
parent/child links are pinned separately by the other tests here, so a rename or a forked
chain fails too.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from pathlib import Path

import app

MIGRATIONS = Path(app.__file__).resolve().parents[2] / "database" / "migrations" / "versions"

#: The nine revisions of the completed platform, in hashing order. Listed rather than
#: discovered: a migration appearing or disappearing should fail this file, not be absorbed
#: by it.
EXPECTED_FILENAMES = (
    "20260828_0001_initial_schema.py",
    "20260828_0002_payments_public_id.py",
    "20260831_0003_users_authentication.py",
    "20260901_0004_user_hotel_membership.py",
    "20260901_0005_platform_administration.py",
    "20260902_0006_users_password_changed_at.py",
    "20260904_0007_audit_events.py",
    "20260904_0008_audit_retention_archive.py",
    "20260905_0009_audit_booking_deleted.py",
)

#: sha256 of the canonicalised concatenation described in this module's docstring. Derived
#: from the nine files above; not a value chosen to make anything pass.
CANONICAL_SHA256 = "0dc2f8b156e87d65827bd8a2d5802e53a3e91625ff3bd449b9d97e1ddc335895"

EXPECTED_HEAD = "0009_audit_booking_deleted"
EXPECTED_ROOT = "0001_initial_schema"

REVISION = re.compile(r'^revision: str = "([^"]+)"', re.MULTILINE)
DOWN_REVISION = re.compile(r'^down_revision: str \| None = (?:None|"([^"]+)")', re.MULTILINE)


def migration_files() -> list[Path]:
    """Every revision file, in the one order this module ever uses."""
    return sorted(MIGRATIONS.glob("*.py"), key=lambda path: path.name)


def canonicalise(raw: bytes) -> bytes:
    """The checkout-independent form of *raw*. Hashing input only; nothing is written."""
    return raw.replace(b"\r\n", b"\n")


def digest_of(contents: Iterable[bytes]) -> str:
    running = hashlib.sha256()
    for raw in contents:
        running.update(canonicalise(raw))
    return running.hexdigest()


def test_the_chain_is_exactly_these_nine_files_in_this_order() -> None:
    assert tuple(path.name for path in migration_files()) == EXPECTED_FILENAMES


def test_the_canonical_digest_is_unchanged() -> None:
    """The assertion the rest of this module exists to make trustworthy."""
    assert digest_of(path.read_bytes() for path in migration_files()) == CANONICAL_SHA256


def test_the_digest_does_not_depend_on_how_git_checked_the_files_out() -> None:
    """The property that makes the constant above shareable between machines.

    Both renderings below hold the same content and differ only in line terminator, which is
    exactly the difference between a Linux checkout and a Windows one. If canonicalisation
    ever stopped happening, one of these would drift and this test would say so -- which a
    plain `assert digest == CONSTANT` on one machine never could.
    """
    as_lf = [path.read_bytes().replace(b"\r\n", b"\n") for path in migration_files()]
    as_crlf = [raw.replace(b"\n", b"\r\n") for raw in as_lf]

    assert as_lf != as_crlf, "the sample must actually differ, or this proves nothing"
    assert digest_of(as_lf) == CANONICAL_SHA256
    assert digest_of(as_crlf) == CANONICAL_SHA256


def test_the_chain_is_linear_with_one_root_and_one_head() -> None:
    """Nine revisions, each naming the previous one, ending where alembic.ini expects."""
    parents: dict[str, str | None] = {}
    for path in migration_files():
        source = path.read_text(encoding="utf-8")
        revision_match = REVISION.search(source)
        down_match = DOWN_REVISION.search(source)
        assert revision_match is not None, f"{path.name} declares no revision"
        assert down_match is not None, f"{path.name} declares no down_revision"
        # group(1) is None for the root, whose down_revision is the literal None.
        parents[revision_match.group(1)] = down_match.group(1)

    assert len(parents) == len(EXPECTED_FILENAMES)

    roots = [revision for revision, parent in parents.items() if parent is None]
    children: dict[str, list[str]] = {}
    for revision, parent in parents.items():
        if parent is not None:
            children.setdefault(parent, []).append(revision)
    heads = [revision for revision in parents if revision not in children]

    assert roots == [EXPECTED_ROOT]
    assert heads == [EXPECTED_HEAD]
    assert all(len(forks) == 1 for forks in children.values()), f"forked chain: {children}"
