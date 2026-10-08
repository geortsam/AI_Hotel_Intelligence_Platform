"""The ``If-Match`` precondition, without a database (Issue H6).

The token is the record's ``updated_at`` as a strong entity-tag, compared by instant and exact
equality. These tests pin the grammar, the canonical form, that no precision is lost on the way
in or out, and which operations document the precondition. The behaviour against PostgreSQL --
the lock, the refusal, the untouched row -- is ``tests/integration/test_optimistic_concurrency.py``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel

from app.api.preconditions import (
    HEADER,
    STALE_UPDATE_RESPONSE,
    entity_tag,
    parse_if_match,
)
from app.core.errors import StaleUpdateError
from app.main import app
from app.services.concurrency import require_unchanged

UTC = dt.UTC
ATHENS = dt.timezone(dt.timedelta(hours=3))
INSTANT = dt.datetime(2026, 10, 9, 10, 0, 0, 123456, tzinfo=UTC)


class Stamped(BaseModel):
    """How every affected response serialises ``updated_at``."""

    updated_at: dt.datetime


# --- the canonical token ------------------------------------------------------------------------


def test_the_canonical_token_is_utc_with_six_digits_and_z() -> None:
    assert entity_tag(INSTANT) == '"2026-10-09T10:00:00.123456Z"'


def test_the_canonical_token_keeps_trailing_zero_microseconds() -> None:
    assert entity_tag(INSTANT.replace(microsecond=0)) == '"2026-10-09T10:00:00.000000Z"'
    assert entity_tag(INSTANT.replace(microsecond=500)) == '"2026-10-09T10:00:00.000500Z"'


def test_one_instant_has_one_canonical_token_whatever_its_offset() -> None:
    assert entity_tag(INSTANT.astimezone(ATHENS)) == entity_tag(INSTANT)


def test_a_naive_timestamp_has_no_token() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        entity_tag(INSTANT.replace(tzinfo=None))


# --- parsing --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "instant",
    [
        INSTANT,
        INSTANT.replace(microsecond=0),
        INSTANT.replace(microsecond=1),
        INSTANT.replace(microsecond=999999),
        INSTANT.astimezone(ATHENS),
    ],
)
def test_the_canonical_token_parses_back_to_the_same_instant(instant: dt.datetime) -> None:
    assert parse_if_match(entity_tag(instant)) == instant


@pytest.mark.parametrize(
    "instant",
    [INSTANT, INSTANT.replace(microsecond=0), INSTANT.replace(microsecond=500)],
)
@pytest.mark.parametrize("zone", [UTC, ATHENS])
def test_the_apis_own_updated_at_string_is_a_token_without_loss(
    instant: dt.datetime, zone: dt.timezone
) -> None:
    """What a client received, quoted, names exactly the instant the server holds -- whether
    the session rendered it in UTC or with an offset, with or without microseconds."""
    serialised = Stamped(updated_at=instant.astimezone(zone)).model_dump(mode="json")
    token = f'"{serialised["updated_at"]}"'

    parsed = parse_if_match(token)

    assert parsed == instant
    assert parsed is not None and parsed.microsecond == instant.microsecond


@pytest.mark.parametrize("value", [None])
def test_no_header_constrains_nothing(value: None) -> None:
    assert parse_if_match(value) is None


@pytest.mark.parametrize("value", ["*", " * ", "*\t"])
def test_a_wildcard_constrains_nothing(value: str) -> None:
    assert parse_if_match(value) is None


def test_surrounding_whitespace_is_ignored() -> None:
    assert parse_if_match(f"  {entity_tag(INSTANT)}  ") == INSTANT


MALFORMED: list[tuple[str, str]] = [
    ('W/"2026-10-09T10:00:00.123456Z"', "weak"),
    ("2026-10-09T10:00:00.123456Z", "exactly one quoted"),
    ('"2026-10-09T10:00:00Z", "2026-10-09T11:00:00Z"', "exactly one quoted"),
    ("", "exactly one quoted"),
    ('"2026-10-09T10:00:00Z', "exactly one quoted"),
    ('""', "not an RFC 3339 timestamp"),
    ('"not-a-time"', "not an RFC 3339 timestamp"),
    ('"2026-13-40T10:00:00Z"', "not an RFC 3339 timestamp"),
    ('"2026-10-09T10:00:00.123456"', "time zone"),
    ('"2026-10-09"', "time zone"),
    ('"2026-10-09T10:00:00.1234567Z"', "more precise than a microsecond"),
]


@pytest.mark.parametrize(("value", "reason"), MALFORMED)
def test_a_malformed_precondition_is_a_validation_error_at_the_header(
    value: str, reason: str
) -> None:
    with pytest.raises(RequestValidationError) as refused:
        parse_if_match(value)

    (error,) = refused.value.errors()
    assert error["loc"] == ("header", HEADER)
    assert error["type"] == "value_error"
    assert reason in error["msg"]


# --- the comparison -------------------------------------------------------------------------------


def test_no_precondition_never_refuses() -> None:
    require_unchanged(INSTANT, None)


def test_the_same_instant_passes_in_any_offset() -> None:
    require_unchanged(INSTANT, INSTANT)
    require_unchanged(INSTANT, INSTANT.astimezone(ATHENS))


@pytest.mark.parametrize(
    "expected",
    [
        INSTANT + dt.timedelta(microseconds=1),
        INSTANT - dt.timedelta(microseconds=1),
        INSTANT + dt.timedelta(days=1),
        INSTANT - dt.timedelta(days=1),
    ],
)
def test_any_other_instant_is_stale_earlier_or_later(expected: dt.datetime) -> None:
    """Equality, not order: the trigger stamps transaction START time, so an older-looking
    stamp can be the newer write. Either way it is not the version the client edited."""
    with pytest.raises(StaleUpdateError) as refused:
        require_unchanged(INSTANT, expected)

    assert refused.value.status_code == 412
    assert refused.value.code == "STALE_UPDATE"


# --- the documented contract ----------------------------------------------------------------------

#: The five operations Issue H6 made conditional: the form-backed resources that carry an
#: ``updated_at``, and the booking's field edits.
CONDITIONAL = {
    ("patch", "/api/v1/hotels/{public_id}"),
    ("patch", "/api/v1/hotels/{hotel_public_id}/guests/{guest_public_id}"),
    ("patch", "/api/v1/hotels/{hotel_public_id}/room-types/{code}"),
    ("patch", "/api/v1/hotels/{hotel_public_id}/room-types/{room_type_code}/rooms/{room_number}"),
    ("patch", "/api/v1/hotels/{hotel_public_id}/bookings/{booking_public_id}"),
}


def operations() -> dict[tuple[str, str], dict[str, Any]]:
    document = app.openapi()
    return {
        (method, path): operation
        for path, item in document["paths"].items()
        for method, operation in item.items()
    }


def test_exactly_the_five_conditional_operations_declare_if_match() -> None:
    declaring = {
        key
        for key, operation in operations().items()
        if any(p["name"] == HEADER for p in operation.get("parameters", []))
    }
    assert declaring == CONDITIONAL


def test_exactly_the_five_conditional_operations_declare_412() -> None:
    declaring = {key for key, operation in operations().items() if "412" in operation["responses"]}
    assert declaring == CONDITIONAL


def test_the_header_is_optional_and_the_412_is_the_shared_error_envelope() -> None:
    for key in CONDITIONAL:
        operation = operations()[key]
        (header,) = [p for p in operation["parameters"] if p["name"] == HEADER]
        assert header["in"] == "header"
        assert header.get("required", False) is False
        response = operation["responses"]["412"]
        assert response["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorResponse"
        }
        assert "STALE_UPDATE" in response["description"]
    assert set(STALE_UPDATE_RESPONSE) == {412}


@pytest.mark.parametrize(
    "excluded",
    [
        ("patch", "/api/v1/amenities/{code}"),
        ("patch", "/api/v1/revenue-categories/{code}"),
        ("patch", "/api/v1/expense-categories/{code}"),
        ("patch", "/api/v1/hotels/{hotel_public_id}/bookings/{booking_public_id}/stay"),
        ("patch", "/api/v1/hotels/{hotel_public_id}/members/{user_public_id}"),
        ("patch", "/api/v1/hotels/{hotel_public_id}/bookings/{booking_public_id}/review"),
    ],
)
def test_catalogues_and_commands_stay_unconditional(excluded: tuple[str, str]) -> None:
    """The catalogues carry no ``updated_at``; the commands are already judged against the
    current state. Neither gains the precondition."""
    operation = operations()[excluded]
    assert all(p["name"] != HEADER for p in operation.get("parameters", []))
    assert "412" not in operation["responses"]
