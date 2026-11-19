"""R1.5: `hotels.timezone` is an IANA zone name, checked at the API edge.

Before R1.5 any text up to 64 characters was stored, and the business day of a hotel whose zone
did not exist was quietly taken in UTC (`app/services/business_day.py`). The create and update
payloads now refuse a name the installed IANA database does not define, exactly as written; the
response model does not check, so a row written before R1.5 still reads. Offline: the database
behaviour is in `tests/integration/test_hotels_api.py`.
"""

from __future__ import annotations

import datetime as dt
import uuid
import zoneinfo
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas import hotel as hotel_schema
from app.schemas.hotel import (
    NOT_A_PLACE,
    HotelCreate,
    HotelResponse,
    HotelUpdate,
    iana_zone_names,
)

REQUIRED: dict[str, Any] = {
    "slug": "acropolis-view",
    "name": "Acropolis View Hotel",
    "address_line1": "1 Dionysiou Areopagitou",
    "city": "Athens",
    "country_code": "GR",
    "currency": "EUR",
}

VALID = ["UTC", "Europe/Athens", "Pacific/Auckland", "America/Los_Angeles", "Etc/GMT+5"]

#: Not IANA names as written: unknown, wrong case, the system's own aliases, path tricks.
INVALID = [
    "Mars/Olympus",
    "Not/AZone",
    "europe/athens",
    "EUROPE/ATHENS",
    "utc",
    "localtime",
    "posixrules",
    "Factory",
    "posix/Europe/Athens",
    "right/UTC",
    "Europe/Athens/",
    "Europe/../UTC",
    "../../etc/passwd",
    "Europe\\Athens",
    "+02:00",
    "EET+2",
]


def refusal(error: ValidationError) -> list[tuple[Any, ...]]:
    return [tuple(e["loc"]) for e in error.errors()]


def test_the_default_is_utc_and_utc_is_an_iana_name() -> None:
    assert HotelCreate(**REQUIRED).timezone == "UTC"
    assert "UTC" in iana_zone_names()


@pytest.mark.parametrize("zone", VALID)
def test_an_iana_name_is_accepted_on_create_and_update(zone: str) -> None:
    assert HotelCreate(**REQUIRED, timezone=zone).timezone == zone
    assert HotelUpdate(timezone=zone).timezone == zone


@pytest.mark.parametrize("zone", INVALID)
def test_a_name_the_iana_database_does_not_define_is_refused_on_create(zone: str) -> None:
    with pytest.raises(ValidationError) as raised:
        HotelCreate(**REQUIRED, timezone=zone)
    assert refusal(raised.value) == [("timezone",)]
    assert "IANA time zone name" in str(raised.value)


@pytest.mark.parametrize("zone", INVALID)
def test_a_name_the_iana_database_does_not_define_is_refused_on_update(zone: str) -> None:
    with pytest.raises(ValidationError) as raised:
        HotelUpdate(timezone=zone)
    assert refusal(raised.value) == [("timezone",)]


@pytest.mark.parametrize("zone", ["", "   ", "A" * 65])
def test_the_length_bounds_still_hold(zone: str) -> None:
    with pytest.raises(ValidationError):
        HotelCreate(**REQUIRED, timezone=zone)


def test_surrounding_whitespace_is_stripped_before_the_check() -> None:
    assert HotelCreate(**REQUIRED, timezone="  Europe/Athens ").timezone == "Europe/Athens"


def test_an_omitted_zone_on_update_is_left_alone() -> None:
    update = HotelUpdate(name="Renamed")
    assert update.timezone is None
    assert "timezone" not in update.model_dump(exclude_unset=True)


def test_the_check_is_the_installed_iana_database() -> None:
    assert iana_zone_names() == frozenset(zoneinfo.available_timezones()) - NOT_A_PLACE
    assert iana_zone_names() is iana_zone_names()


def test_a_host_alias_is_refused_even_where_the_host_lists_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Debian's zone directory holds `localtime`; Windows' `tzdata` package does not.

    So the refusal cannot rest on what this machine happens to list: given a host that lists
    every name in NOT_A_PLACE, each is still refused.
    """
    monkeypatch.setattr(hotel_schema.zoneinfo, "available_timezones", lambda: {"UTC", *NOT_A_PLACE})
    iana_zone_names.cache_clear()
    try:
        assert iana_zone_names() == frozenset({"UTC"})
        for name in sorted(NOT_A_PLACE):
            with pytest.raises(ValidationError):
                HotelUpdate(timezone=name)
    finally:
        monkeypatch.undo()
        iana_zone_names.cache_clear()


def test_every_iana_name_is_accepted() -> None:
    """Not a sample: the whole database, so no place's zone is refused by a stricter rule."""
    refused = []
    for zone in sorted(zoneinfo.available_timezones() - NOT_A_PLACE):
        if len(zone) > 64:
            continue
        try:
            HotelUpdate(timezone=zone)
        except ValidationError:
            refused.append(zone)
    assert refused == []


def test_a_row_written_before_r1_5_still_reads() -> None:
    """The response model does not check: an unknown stored zone is returned as stored."""
    now = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)
    response = HotelResponse(
        public_id=uuid.uuid4(),
        slug="legacy",
        name="Legacy",
        address_line1="1 Street",
        address_line2=None,
        city="Athens",
        region=None,
        postal_code=None,
        country_code="GR",
        latitude=None,
        longitude=None,
        email=None,
        phone=None,
        website=None,
        timezone="Not/AZone",
        currency="EUR",
        star_rating=None,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    assert response.timezone == "Not/AZone"
