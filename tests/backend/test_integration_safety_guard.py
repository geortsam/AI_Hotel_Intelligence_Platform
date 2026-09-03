"""Tests for the integration-suite safety guard.

The guard is the only thing standing between a typo in ``TEST_DATABASE_URL`` and a dropped
production schema, so it is tested directly. Every function under test is pure string
analysis -- **no test in this file opens a database connection**, which is why they live in
``tests/backend/`` and run in every environment.
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import (
    REQUIRED_DB_SUFFIX,
    UnsafeTestDatabaseError,
    assert_safe_test_database_url,
    database_name_from_url,
    redact_url,
)

PASSWORD = "sup3r-s3cret-pa55word"


def url_for(database: str, *, host: str = "localhost", password: str = PASSWORD) -> str:
    return f"postgresql+psycopg://tester:{password}@{host}:5432/{database}"


# --- accepted ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "database",
    ["hotel_test", "hotel_intelligence_test", "ai_hotel_test", "x_test", "Hotel_TEST"],
)
def test_valid_test_databases_are_accepted(database: str) -> None:
    assert assert_safe_test_database_url(url_for(database)) == database


def test_accepts_a_url_with_query_parameters() -> None:
    url = f"postgresql+psycopg://tester:{PASSWORD}@localhost:5432/hotel_test?sslmode=require"
    assert assert_safe_test_database_url(url) == "hotel_test"


def test_accepts_a_url_with_no_password() -> None:
    """Trust-authenticated local servers are legitimate; the guard checks names, not auth."""
    assert assert_safe_test_database_url("postgresql://tester@localhost:5432/hotel_test") == (
        "hotel_test"
    )


# --- refused: production-like and ordinary names --------------------------------------


@pytest.mark.parametrize(
    "database",
    [
        "hotel_intelligence_prod",
        "production",
        "hotel_production",
        "hotel_intelligence_live",
    ],
)
def test_production_like_databases_are_refused(database: str) -> None:
    with pytest.raises(UnsafeTestDatabaseError, match="UNSAFE"):
        assert_safe_test_database_url(url_for(database))


@pytest.mark.parametrize("database", ["hotel", "hotel_intelligence", "postgres", "main"])
def test_ordinary_databases_are_refused(database: str) -> None:
    with pytest.raises(UnsafeTestDatabaseError, match="UNSAFE"):
        assert_safe_test_database_url(url_for(database))


@pytest.mark.parametrize("database", ["test", "test_hotel", "hotel_tests", "hoteltest"])
def test_near_misses_are_refused(database: str) -> None:
    """The rule is a suffix, not a substring: `test_hotel` and `hotel_tests` do not pass."""
    with pytest.raises(UnsafeTestDatabaseError):
        assert_safe_test_database_url(url_for(database))


def test_refusal_names_the_detected_database_and_the_convention() -> None:
    with pytest.raises(UnsafeTestDatabaseError) as excinfo:
        assert_safe_test_database_url(url_for("hotel_intelligence"))

    message = str(excinfo.value)
    assert "hotel_intelligence" in message  # what was detected
    assert REQUIRED_DB_SUFFIX in message  # the required convention
    assert "hotel_test" in message  # a valid example
    assert "refuses to run" in message


# --- refused: dangerous hosts ---------------------------------------------------------


@pytest.mark.parametrize("host", ["db.prod.internal", "production-db", "live-cluster.example.com"])
def test_dangerous_hosts_are_refused_even_with_a_test_database_name(host: str) -> None:
    with pytest.raises(UnsafeTestDatabaseError, match="host looks like real infrastructure"):
        assert_safe_test_database_url(url_for("hotel_test", host=host))


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "db", "postgres-ci"])
def test_ordinary_hosts_are_accepted(host: str) -> None:
    assert assert_safe_test_database_url(url_for("hotel_test", host=host)) == "hotel_test"


# --- refused: malformed URLs ----------------------------------------------------------


@pytest.mark.parametrize("url", ["", "   ", "\t\n"])
def test_empty_urls_are_refused(url: str) -> None:
    with pytest.raises(UnsafeTestDatabaseError, match="empty"):
        assert_safe_test_database_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "localhost:5432/hotel_test",  # urlsplit reads "localhost" as the scheme
        "sqlite:///hotel_test",  # wrong engine entirely
        "mysql://tester@localhost/hotel_test",
    ],
)
def test_url_without_a_postgres_scheme_is_refused(url: str) -> None:
    """A bare host:port/db parses with the HOST as the scheme, so merely checking that a
    scheme exists would wave it through."""
    with pytest.raises(UnsafeTestDatabaseError, match="not a PostgreSQL URL"):
        assert_safe_test_database_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://tester@localhost:5432",  # no database at all
        "postgresql+psycopg://tester@localhost:5432/",  # empty database name
    ],
)
def test_url_without_a_database_name_is_refused(url: str) -> None:
    with pytest.raises(UnsafeTestDatabaseError, match="names no database"):
        assert_safe_test_database_url(url)


def test_url_with_an_invalid_port_is_refused_rather_than_assumed_safe() -> None:
    """An unparseable URL is refused, not given the benefit of the doubt: if we cannot tell
    what we are about to drop, we do not drop it."""
    with pytest.raises(UnsafeTestDatabaseError, match="could not be parsed"):
        assert_safe_test_database_url(
            "postgresql+psycopg://tester:pw@localhost:not-a-port/hotel_test"
        )


# --- the password never escapes -------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        url_for("hotel_intelligence"),
        url_for("production"),
        url_for("hotel_test", host="db.prod.internal"),
        f"postgresql+psycopg://tester:{PASSWORD}@localhost:not-a-port/hotel_test",
        f"postgresql+psycopg://tester:{PASSWORD}@localhost:5432/",
    ],
)
def test_password_never_appears_in_any_error_message(url: str) -> None:
    """Errors reach terminals, logs and CI transcripts. None of them may carry the secret."""
    with pytest.raises(UnsafeTestDatabaseError) as excinfo:
        assert_safe_test_database_url(url)

    assert PASSWORD not in str(excinfo.value)


def test_redact_url_masks_the_password_but_keeps_the_target_legible() -> None:
    redacted = redact_url(url_for("hotel_test"))

    assert PASSWORD not in redacted
    assert "***" in redacted
    # Everything needed to identify the target survives.
    assert "tester" in redacted
    assert "localhost" in redacted
    assert "5432" in redacted
    assert "hotel_test" in redacted


def test_redact_url_leaves_a_passwordless_url_alone() -> None:
    url = "postgresql://tester@localhost:5432/hotel_test"
    assert redact_url(url) == url


def test_redact_url_never_raises_on_a_malformed_url() -> None:
    """Redaction is called from inside error paths; it must not fail there."""
    assert redact_url("postgresql://h:not-a-port/db") == "<unparseable URL>"


# --- helper behaviour -----------------------------------------------------------------


def test_database_name_extraction() -> None:
    assert database_name_from_url(url_for("hotel_test")) == "hotel_test"
    assert database_name_from_url("postgresql://h/anything") == "anything"
