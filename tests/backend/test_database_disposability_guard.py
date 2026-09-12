"""Tests for the second safety signal: is the target database actually disposable?

`test_integration_safety_guard.py` covers the first signal -- the URL's shape and name. This
file covers the one added in Stage 5.17, and the combination of the two.

**No test in this file opens a database connection.** The decision is a pure function over
facts, and the facts are supplied by a fake reader, so a destructive-operation test can prove
the operation was refused without any database being at risk. That is the point: a safety
test that needed a real database to prove it would be the most dangerous test in the suite.
"""

from __future__ import annotations

import pytest

from tests.db_safety import (
    DISPOSABLE_MARKER,
    DatabaseContents,
    UnsafeTestDatabaseError,
    assert_disposable_database,
    assert_safe_destructive_target,
    evaluate_disposability,
)

PASSWORD = "sup3r-s3cret-pa55word"

#: The exact name this repository's CI container AND its seeded demo database both use. It
#: is the reason a name cannot be the only signal.
SHARED_NAME = "hotel_intelligence_test"


def url_for(database: str = SHARED_NAME, *, host: str = "localhost") -> str:
    return f"postgresql+psycopg://tester:{PASSWORD}@{host}:5432/{database}"


class RecordingReader:
    """A reader that returns fixed facts and records that it was consulted.

    Whether it was called *at all* is half of what these tests assert: a target rejected on
    its name must never reach a connection.
    """

    def __init__(self, contents: DatabaseContents) -> None:
        self.contents = contents
        self.calls: list[str] = []

    def __call__(self, url: str) -> DatabaseContents:
        self.calls.append(url)
        return self.contents


def reader_for(contents: DatabaseContents) -> RecordingReader:
    return RecordingReader(contents)


EMPTY = DatabaseContents(marker=None, row_counts={})
MIGRATED_BUT_EMPTY = DatabaseContents(
    marker=None, row_counts={"hotels": 0, "users": 0, "bookings": 0}
)
#: The real demo database, as it actually stood when this stage was written.
DEMO_LIKE = DatabaseContents(
    marker=None, row_counts={"hotels": 2, "users": 2, "bookings": 166, "revenue": 118}
)


# --- accepted -------------------------------------------------------------------------


def test_a_freshly_created_database_is_disposable() -> None:
    """No application tables at all: nothing to lose. The ordinary CI case."""
    evaluate_disposability(SHARED_NAME, url_for(), EMPTY)


def test_a_migrated_but_empty_database_is_disposable() -> None:
    """Schema present, no rows. What a scratch database looks like after the suite's own
    teardown, so a second run is not blocked."""
    evaluate_disposability(SHARED_NAME, url_for(), MIGRATED_BUT_EMPTY)


def test_a_populated_database_marked_disposable_is_accepted() -> None:
    """The deliberate opt-in: someone has decided this database is expendable."""
    evaluate_disposability(
        "my_scratch_test",
        url_for("my_scratch_test"),
        DatabaseContents(marker=DISPOSABLE_MARKER, row_counts={"hotels": 9}),
    )


def test_the_marker_may_carry_surrounding_context() -> None:
    evaluate_disposability(
        "my_scratch_test",
        url_for("my_scratch_test"),
        DatabaseContents(
            marker=f"scratch for alice -- {DISPOSABLE_MARKER} -- delete after 2026-10",
            row_counts={"bookings": 3},
        ),
    )


# --- refused --------------------------------------------------------------------------


def test_the_populated_demo_database_is_refused_despite_its_test_name() -> None:
    """The exact scenario this stage exists for.

    `hotel_intelligence_test` passes every name-based rule -- it ends with `_test`, the host
    is localhost -- and it is the database holding the seeded demo data.
    """
    with pytest.raises(UnsafeTestDatabaseError) as exc:
        evaluate_disposability(SHARED_NAME, url_for(), DEMO_LIKE)

    message = str(exc.value)
    assert "already holds application data" in message
    # The facts that change a developer's mind.
    assert "bookings=166" in message
    assert "hotels=2" in message
    assert SHARED_NAME in message


def test_a_single_row_is_enough_to_refuse() -> None:
    with pytest.raises(UnsafeTestDatabaseError):
        evaluate_disposability(
            SHARED_NAME, url_for(), DatabaseContents(marker=None, row_counts={"guests": 1})
        )


def test_an_unrelated_database_comment_does_not_count_as_the_marker() -> None:
    with pytest.raises(UnsafeTestDatabaseError):
        evaluate_disposability(
            SHARED_NAME,
            url_for(),
            DatabaseContents(
                marker="the main demo database, do not drop", row_counts={"hotels": 2}
            ),
        )


def test_the_refusal_states_that_nothing_was_executed() -> None:
    with pytest.raises(UnsafeTestDatabaseError) as exc:
        evaluate_disposability(SHARED_NAME, url_for(), DEMO_LIKE)

    message = str(exc.value)
    assert "were NOT executed" in message
    assert "downgrade base" in message


def test_the_refusal_explains_both_ways_forward() -> None:
    with pytest.raises(UnsafeTestDatabaseError) as exc:
        evaluate_disposability(SHARED_NAME, url_for(), DEMO_LIKE)

    message = str(exc.value)
    # Mark it, or point somewhere else. Both, with the literal SQL.
    assert "COMMENT ON DATABASE" in message
    assert DISPOSABLE_MARKER in message
    assert "postgresql+psycopg://" in message


# --- no credential ever reaches a message ----------------------------------------------


def test_password_never_appears_in_a_disposability_refusal() -> None:
    with pytest.raises(UnsafeTestDatabaseError) as exc:
        evaluate_disposability(SHARED_NAME, url_for(), DEMO_LIKE)

    assert PASSWORD not in str(exc.value)
    assert "***" in str(exc.value)


# --- the two signals together ----------------------------------------------------------


def test_both_signals_run_and_the_name_rule_comes_first() -> None:
    """A bad name is refused without the database being touched at all.

    Order matters: an unreachable or mistyped host should produce the naming error, not a
    connection error, and no connection should be attempted for a target already known bad.
    """
    reader = reader_for(EMPTY)
    with pytest.raises(UnsafeTestDatabaseError, match="must end with"):
        assert_safe_destructive_target(url_for("hotel_intelligence"), reader=reader)

    assert reader.calls == []


def test_a_good_name_with_bad_contents_is_still_refused() -> None:
    reader = reader_for(DEMO_LIKE)
    with pytest.raises(UnsafeTestDatabaseError, match="already holds application data"):
        assert_safe_destructive_target(url_for(), reader=reader)

    # The contents WERE consulted -- the name alone did not settle it.
    assert reader.calls == [url_for()]


def test_a_safe_target_clears_both_signals_and_returns_its_name() -> None:
    reader = reader_for(MIGRATED_BUT_EMPTY)
    assert assert_safe_destructive_target(url_for("ci_scratch_test"), reader=reader) == (
        "ci_scratch_test"
    )


def test_assert_disposable_database_rejects_a_url_it_cannot_parse() -> None:
    """It re-derives the name itself, so it cannot be handed a target it never validated."""
    with pytest.raises(UnsafeTestDatabaseError):
        assert_disposable_database("not a url at all", reader=reader_for(EMPTY))


# --- the guard fails closed ------------------------------------------------------------


def test_a_database_that_cannot_be_inspected_is_refused() -> None:
    """A target the guard cannot read is one it will not let the suite drop."""

    def exploding_reader(url: str) -> DatabaseContents:
        raise UnsafeTestDatabaseError("could not connect")

    with pytest.raises(UnsafeTestDatabaseError):
        assert_disposable_database(url_for(), reader=exploding_reader)


def test_the_real_reader_is_used_when_none_is_injected() -> None:
    """The production path does not quietly default to something permissive.

    Proven without a database: an unroutable target makes the real reader fail, and the
    failure is a refusal rather than a pass.
    """
    with pytest.raises(UnsafeTestDatabaseError) as exc:
        assert_disposable_database(
            "postgresql+psycopg://tester:pw@127.0.0.1:1/never_listening_test"
        )

    message = str(exc.value)
    assert "could not be inspected" in message
    assert "nothing destructive was executed" in message
