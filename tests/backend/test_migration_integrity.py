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

#: The ten revisions of the platform, in hashing order. Listed rather than discovered: a
#: migration appearing or disappearing should fail this file, not be absorbed by it -- which is
#: exactly what happened when Stage 6.8 added the tenth, and is the point.
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
    "20260920_0010_demand_predictions.py",
    "20260921_0011_demand_prediction_public_id.py",
    "20260924_0012_audit_tool_invoked.py",
    "20260925_0013_llm_invocations.py",
    "20260926_0014_hotel_documents.py",
    "20260927_0015_copilot_conversations.py",
    "20261002_0016_demand_observation_periods.py",
    "20261003_0017_llm_invocation_retention.py",
    "20261006_0018_composite_set_null_columns.py",
    "20261007_0019_review_external_id_scope.py",
    "20261009_0020_guest_email_rules.py",
    "20261119_0021_audit_platform_grants.py",
    "20261120_0022_catalogue_versions.py",
    "20261120_0023_auth_sessions.py",
)

#: sha256 of the canonicalised concatenation described in this module's docstring. Derived
#: from the twenty-three files above; not a value chosen to make anything pass.
#:
#: Moved on every migration. `0dc2f8b1...` over nine files was the value before Stage 6.8 added
#: `demand_predictions`; `35162fde...` over ten was the value before Stage 6.11 added
#: `demand_predictions.public_id`; `5104c312...` over eleven was the value before Stage 7.6
#: added 0012 (the `tool.invoked` audit vocabulary) -- and recomputing it over the first eleven
#: files still reproduces it, which is the check that 0001-0011 were not edited; and
#: `4706f2d1...` over twelve was the value before Stage 7.7 added 0013 (`llm_invocations`),
#: likewise still reproduced by the first twelve; and `2bf0ffb1...` over thirteen was the value
#: before Stage 7.9 added 0014 (`hotel_documents`), still reproduced by the first thirteen; and
#: `bb93f2b5...` over fourteen was the value before Stage 7.11 added 0015
#: (`copilot_conversations`), still reproduced by the first fourteen; and `31b7886c...` over fifteen
#: was the value before 0016 added `demand_observation_periods`, still reproduced by the first
#: fifteen; and `5c20fc15...` over sixteen was the value before 0017 gave `llm_invocations` its
#: retention rule, still reproduced by the first sixteen; and `690a0cf9...` over seventeen was
#: the value before 0018 (Issue H3) recreated the three composite SET NULL keys with column
#: lists, still reproduced by the first seventeen; and `745f856d...` over eighteen was the value
#: before 0019 (Issue H4) scoped the reviews' external-identifier key to the hotel, still
#: reproduced by the first eighteen; and `e941eb8d...` over nineteen was the value before
#: 0020 (Issue H7) gave guest emails a format CHECK and a case-insensitive key, still
#: reproduced by the first nineteen; and `7074c79d...` over twenty was the value before 0021
#: (R1.3a) widened the audit action CHECK for the platform-administrator command, still
#: reproduced by the first twenty; and `dc753149...` over twenty-one was the value before 0022
#: (R3.3) gave the three global catalogues an `updated_at`, still reproduced by the first
#: twenty-one; and `bf415b16...` over twenty-two was the value before 0023 (R1.1) added
#: `auth_sessions`, still reproduced by the first twenty-two. All fourteen are recorded rather
#: than discarded,
#: because "the digest changed" should always be answerable with "yes, in that commit, for
#: that migration".
CANONICAL_SHA256 = "794d256d068ca0c6ef36cbd31f25afe2ef8d169fce9031d76c5f3f00e66ea0a3"

EXPECTED_HEAD = "0023_auth_sessions"
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


def test_the_chain_is_exactly_these_ten_files_in_this_order() -> None:
    assert tuple(path.name for path in migration_files()) == EXPECTED_FILENAMES


def test_the_canonical_digest_is_unchanged() -> None:
    """The assertion the rest of this module exists to make trustworthy."""
    assert digest_of(path.read_bytes() for path in migration_files()) == CANONICAL_SHA256


def test_stage_7_6_left_every_earlier_migration_untouched() -> None:
    """Adding 0012 moved the digest; this proves it moved ONLY because 0012 was added.

    The eleven files that preceded it still hash to the value pinned before Stage 7.6, so no
    applied revision was edited to make room for the new one.
    """
    earlier = [path.read_bytes() for path in migration_files()[:11]]

    assert digest_of(earlier) == "5104c312275dd5975f7097a9d8957588751ec03608b43089a4011adb5a5a1a30"


def test_stage_7_7_left_every_earlier_migration_untouched() -> None:
    """Adding 0013 moved the digest only because 0013 was added: 0001-0012 are byte-identical."""
    earlier = [path.read_bytes() for path in migration_files()[:12]]

    assert digest_of(earlier) == "4706f2d1ea2b6eba9e483426747961ee3df3ff402d615c08d0b8029c687acedf"


def test_stage_7_9_left_every_earlier_migration_untouched() -> None:
    """Adding 0014 moved the digest only because 0014 was added: 0001-0013 are byte-identical.

    0014 widens the audit vocabulary by replacing two CHECK constraints -- an ALTER of an
    applied table, done by a new revision, never by editing 0007, 0009 or 0012.
    """
    earlier = [path.read_bytes() for path in migration_files()[:13]]

    assert digest_of(earlier) == "2bf0ffb1a514c7670110d85ad0b626908f5398518b221e946c2222a077881ca1"


def test_stage_7_11_left_every_earlier_migration_untouched() -> None:
    """Adding 0015 moved the digest only because 0015 was added: 0001-0014 are byte-identical.

    0015 creates two tables and alters none, so nothing earlier had any reason to change.
    """
    earlier = [path.read_bytes() for path in migration_files()[:14]]

    assert digest_of(earlier) == "bb93f2b53bc86f50dffc6ead5a278f3d2f98d2ffc8dee5a926efe22ef7df3bc6"


def test_declared_observation_left_every_earlier_migration_untouched() -> None:
    """Adding 0016 moved the digest only because 0016 was added: 0001-0015 are byte-identical.

    0016 creates one table and alters none, so nothing earlier had any reason to change.
    """
    earlier = [path.read_bytes() for path in migration_files()[:15]]

    assert digest_of(earlier) == "31b7886cb805554c57f2fc92458c07ed92b82fea6f29bb854429132b244bf54b"


def test_llm_invocation_retention_left_every_earlier_migration_untouched() -> None:
    """Adding 0017 moved the digest only because 0017 was added: 0001-0016 are byte-identical.

    0017 replaces the body of a trigger function 0013 created -- by a new revision, never by
    editing 0013 -- so 0013's bytes, like every other earlier file's, are unchanged.
    """
    earlier = [path.read_bytes() for path in migration_files()[:16]]

    assert digest_of(earlier) == "5c20fc157cc78112a484b95fd751b22dbfc9f3913e2dc41c902e1d92c2e383a5"


def test_the_set_null_correction_left_every_earlier_migration_untouched() -> None:
    """Adding 0018 moved the digest only because 0018 was added: 0001-0017 are byte-identical.

    0018 recreates three foreign keys 0001 declared -- by a new revision, never by editing
    0001 -- so 0001's bytes, like every other earlier file's, are unchanged.
    """
    earlier = [path.read_bytes() for path in migration_files()[:17]]

    assert digest_of(earlier) == "690a0cf9ced1f9e3c4b7a26980789449069b172b217c5a36781a59ccba11ea01"


def test_the_tenant_scope_correction_left_every_earlier_migration_untouched() -> None:
    """Adding 0019 moved the digest only because 0019 was added: 0001-0018 are byte-identical.

    0019 replaces the external-identifier index 0001 declared -- by a new revision, never by
    editing 0001 -- so 0001's bytes, like every other earlier file's, are unchanged.
    """
    earlier = [path.read_bytes() for path in migration_files()[:18]]

    assert digest_of(earlier) == "745f856d31b91f78a1204bed47ecf9cbf2177cf895381c40df56672a643f27cf"


def test_the_guest_email_rules_left_every_earlier_migration_untouched() -> None:
    """Adding 0020 moved the digest only because 0020 was added: 0001-0019 are byte-identical.

    0020 replaces the guests' email index 0001 declared and adds a CHECK -- by a new revision,
    never by editing 0001 -- so 0001's bytes, like every other earlier file's, are unchanged.
    """
    earlier = [path.read_bytes() for path in migration_files()[:19]]

    assert digest_of(earlier) == "e941eb8dff069b7a4fd97781e188a4f94d3cbea5c9f56273b5dc2dd41b5913d5"


def test_the_platform_grant_actions_left_every_earlier_migration_untouched() -> None:
    """Adding 0021 moved the digest only because 0021 was added: 0001-0020 are byte-identical.

    0021 widens the audit action CHECK as 0009, 0012 and 0014 did -- by a new revision, never by
    editing one of them.
    """
    earlier = [path.read_bytes() for path in migration_files()[:20]]

    assert digest_of(earlier) == "7074c79d3c4f5975ebbbe09c9d1724b9a6660ea90323dad24e51da4ec3b2dea2"


def test_the_catalogue_versions_left_every_earlier_migration_untouched() -> None:
    """Adding 0022 moved the digest only because 0022 was added: 0001-0021 are byte-identical."""
    earlier = [path.read_bytes() for path in migration_files()[:21]]

    assert digest_of(earlier) == "dc753149d62affbff1a65e5839d2f1c8255124dea461f1bbaac612e39bbe0550"


def test_the_auth_sessions_table_left_every_earlier_migration_untouched() -> None:
    """Adding 0023 moved the digest only because 0023 was added: 0001-0022 are byte-identical."""
    earlier = [path.read_bytes() for path in migration_files()[:22]]

    assert digest_of(earlier) == "bf415b16a63f2bc0a51e99c59184d5d9c262b86daa5bc4cc740721741866ea6e"


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
