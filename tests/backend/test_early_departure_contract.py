"""What the early departure promises outside its own behaviour (Issue H2).

``tests/integration/test_early_departure.py`` proves what a departure does to a stay. This file
proves the surroundings: the accuracy protocol is untouched and says where the departure's
bound comes from, the bound IS the protocol's constant, and the OpenAPI document describes both
routes and the plain check-out refusal.
"""

from __future__ import annotations

from typing import Any

import pytest

import app.ml.accuracy_protocol as accuracy_protocol
from app.core.config import Settings
from app.main import create_app
from app.ml.accuracy_protocol import PROTOCOL, SETTLEMENT_LAG_DAYS
from app.models.enums import BOOKING_STATUS_TRANSITIONS, SAFE_AUDIT_DETAIL_KEYS
from app.services.booking import DEPARTURE_LOOKBACK_DAYS
from tests.mutation.harness import REPOSITORY_ROOT

BOOKING = "/api/v1/hotels/{hotel_public_id}/bookings/{booking_public_id}"
DEPARTURE = f"{BOOKING}/stay/departure"
BOUNDARY = "can remove only nights within the most recent 28 days or later"
PURPOSE = "so already-scored accuracy periods are not mutated"


@pytest.fixture(scope="module")
def openapi() -> dict[str, Any]:
    document: dict[str, Any] = create_app(Settings(environment="test")).openapi()
    return document


def test_the_lookback_is_the_accuracy_protocols_settlement_lag() -> None:
    assert DEPARTURE_LOOKBACK_DAYS == SETTLEMENT_LAG_DAYS == 28


def test_the_accuracy_protocol_is_unchanged() -> None:
    """D7: only the description changed. The values, and so the checksum, did not."""
    assert PROTOCOL.checksum == "24426f0ecd1e1d8b97d7c726dc0a4f79184fc3ef4baddb96586d1b546c2e719e"
    assert PROTOCOL.volatile_occupancy_statuses == ("confirmed", "pending")
    assert BOOKING_STATUS_TRANSITIONS["checked_in"] == {"checked_in", "checked_out"}


def test_the_protocol_states_the_departure_boundary_and_why() -> None:
    measurement = (REPOSITORY_ROOT / "docs" / "ml-accuracy-measurement.md").read_text(
        encoding="utf-8"
    )
    for text in (
        " ".join((accuracy_protocol.__doc__ or "").split()),
        " ".join(measurement.split()),
    ):
        assert BOUNDARY in text
        assert PURPOSE in text


def test_the_audit_trail_may_record_how_many_nights_left() -> None:
    assert "nights_removed" in SAFE_AUDIT_DETAIL_KEYS


def test_the_departure_takes_one_date_and_answers_with_the_stay_and_its_money(
    openapi: dict[str, Any],
) -> None:
    post = openapi["paths"][DEPARTURE]["post"]
    body = post["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    request = openapi["components"]["schemas"][body.rsplit("/", 1)[-1]]

    assert set(request["properties"]) == {"departure_date"}
    assert request["required"] == ["departure_date"]
    assert request["additionalProperties"] is False
    assert post["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/StayModificationResponse"
    )
    assert {"401", "403", "404", "409", "422"} <= set(post["responses"])
    assert "earlier than the planned check-out" in post["responses"]["409"]["description"]


def test_the_preview_takes_an_optional_date_and_answers_with_the_range(
    openapi: dict[str, Any],
) -> None:
    get = openapi["paths"][DEPARTURE]["get"]
    [parameter] = [p for p in get["parameters"] if p["in"] == "query"]
    preview = openapi["components"]["schemas"]["StayDeparturePreview"]

    assert (parameter["name"], parameter["required"]) == ("departure_date", False)
    assert set(preview["required"]) == {
        "departure_date",
        "earliest_departure_date",
        "latest_departure_date",
        "planned_check_out_date",
        "nights_removed",
        "repricing",
    }
    assert {"401", "403", "404", "409", "422"} <= set(get["responses"])


def test_a_plain_check_out_names_the_departure_route_in_its_409(openapi: dict[str, Any]) -> None:
    patch = openapi["paths"][BOOKING]["patch"]

    assert "stay/departure" in patch["responses"]["409"]["description"]
    assert "stay/departure" in patch["description"]
