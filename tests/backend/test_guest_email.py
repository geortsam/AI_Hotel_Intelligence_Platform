"""A guest's email address at the API edge (Issue H7), without a database.

The schema checks the shape ``users.email`` has, keeps the 3..254 length bounds, trims the
surrounding whitespace and stores the address lower case, so that the hotel's one-address rule
is case-insensitive. A null still means "no address", and on update "clear it". The same
pattern is enforced by the database (``ck_guests_email_format``, migration 0020) and mirrored by
the frontend form; the last test pins that all of them are one rule. The database's side --
the CHECK, the case-insensitive key, the migration's pre-checks -- is
``tests/integration/test_guest_email_rules.py``.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import CheckConstraint

from app.models.guest import Guest
from app.models.user import User
from app.schemas.guest import EMAIL_PATTERN, GuestCreate, GuestUpdate

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (
    REPO_ROOT / "database" / "migrations" / "versions" / "20261009_0020_guest_email_rules.py"
)
GUEST_FORM = REPO_ROOT / "frontend" / "src" / "features" / "guests" / "GuestForm.tsx"


def create(email: object) -> GuestCreate:
    return GuestCreate(first_name="Ada", last_name="Lovelace", email=email)


def error_types(email: object) -> list[str]:
    with pytest.raises(ValidationError) as refused:
        create(email)
    errors = refused.value.errors()
    assert {error["loc"] for error in errors} == {("email",)}
    return [str(error["type"]) for error in errors]


# --- the shape -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "ada@example.com",
        "a@b.c",
        "first.last+tag@mail.example.co.uk",
        "o'brien@example.ie",
        "ünïcode@exämple.de",
    ],
)
def test_an_address_of_the_right_shape_is_accepted(address: str) -> None:
    assert create(address).email == address.lower()


@pytest.mark.parametrize(
    "address",
    [
        "not-an-email",
        "ada@example",
        "@example.com",
        "ada@",
        "ada@.",
        "ada@example.",
        "ada@@example.com",
        "ada lovelace@example.com",
        "ada@exa mple.com",
        "ada@example\tcom.gr",
    ],
)
def test_a_malformed_address_is_refused_at_the_field(address: str) -> None:
    assert error_types(address) == ["string_pattern_mismatch"]


def test_the_shape_is_checked_on_update_too() -> None:
    with pytest.raises(ValidationError) as refused:
        GuestUpdate(email="not-an-email")
    assert [error["loc"] for error in refused.value.errors()] == [("email",)]


# --- the length bounds ---------------------------------------------------------------------------


def test_the_longest_address_the_column_allows_is_accepted() -> None:
    address = "a" * 64 + "@" + "b" * 185 + ".com"
    assert len(address) == 254

    assert create(address).email == address


def test_one_character_longer_is_refused() -> None:
    address = "a" * 65 + "@" + "b" * 185 + ".com"
    assert len(address) == 255

    assert error_types(address) == ["string_too_long"]


def test_shorter_than_three_characters_is_refused() -> None:
    assert error_types("a@") == ["string_too_short"]


# --- normalisation --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("Ada@Example.COM", "ada@example.com"),
        ("ADA.LOVELACE@EXAMPLE.TEST", "ada.lovelace@example.test"),
        ("  ada@example.com\t", "ada@example.com"),
        ("\n Ada@Example.com  ", "ada@example.com"),
    ],
)
def test_an_address_is_trimmed_and_lower_cased_on_create_and_update(
    typed: str, stored: str
) -> None:
    assert create(typed).email == stored
    assert GuestUpdate(email=typed).email == stored


def test_whitespace_inside_an_address_is_refused_not_removed() -> None:
    assert error_types(" ada @example.com ") == ["string_pattern_mismatch"]


# --- no address -----------------------------------------------------------------------------------


def test_an_address_is_optional_on_create() -> None:
    assert GuestCreate(first_name="Ada", last_name="Lovelace").email is None
    assert create(None).email is None


def test_an_explicit_null_clears_on_update_and_an_omission_leaves_it() -> None:
    assert GuestUpdate(email=None).model_dump(exclude_unset=True) == {"email": None}
    assert "email" not in GuestUpdate(notes="x").model_dump(exclude_unset=True)


def test_an_empty_string_is_not_a_way_to_clear_it() -> None:
    """Clearing is an explicit null; an empty string is a malformed address (it was also a
    422 before Issue H7, by the length bound)."""
    assert error_types("") == ["string_too_short"]
    assert error_types("   ") == ["string_too_short"]


# --- one rule, everywhere -------------------------------------------------------------------------


def check_pattern(sqltext: str) -> str:
    match = re.search(r"~ '([^']*)'", sqltext)
    assert match is not None, sqltext
    return match.group(1)


def test_the_api_the_database_the_frontend_and_users_share_one_pattern() -> None:
    """``ck_users_email_format`` is the source; every other copy must equal it exactly."""
    users = next(
        check_pattern(str(c.sqltext))
        for c in User.metadata.tables["users"].constraints
        if isinstance(c, CheckConstraint) and c.name == "ck_users_email_format"
    )
    guests = next(
        str(c.sqltext)
        for c in Guest.metadata.tables["guests"].constraints
        if isinstance(c, CheckConstraint) and c.name == "ck_guests_email_format"
    )
    spec = importlib.util.spec_from_file_location("migration_0020", MIGRATION)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    frontend = re.search(
        r"export const EMAIL_PATTERN = /(.*)/\n", GUEST_FORM.read_text(encoding="utf-8")
    )
    assert frontend is not None

    assert users == r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    assert users == EMAIL_PATTERN
    assert guests == f"email IS NULL OR email ~ '{users}'"
    assert users == migration.EMAIL_FORMAT
    assert frontend.group(1) == users
