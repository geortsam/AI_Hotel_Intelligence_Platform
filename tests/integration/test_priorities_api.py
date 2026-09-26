"""Stage 7.12 -- the attention list over real PostgreSQL and the real dependency chain.

Real: authentication, the access policy and scope resolver, `AnalyticsRepository`'s SQL, the
intelligence service the list composes, the tool invocation boundary and its audit events.
Scripted, for the copilot test only: the language model.

Two hotels with different histories (A: one room, busy on Saturdays; B: three rooms, busy on
Mondays), so a list built from the wrong hotel's data would show the wrong days.

    viewer_a   viewer of A      owner_b   owner of B      outsider   a member of nothing
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.api.deps import get_chat_model
from app.llm.base import ChatModel, ToolCall
from app.llm.testing import ScriptedModel, ScriptedTurn
from app.models.hotel import Hotel
from app.models.room import Room
from tests.integration.conftest import (
    allocate_room,
    create_test_app,
    grant_membership,
    make_booking,
    make_guest,
    make_hotel,
    make_room,
    make_room_type,
    price_nights,
    register_and_login,
)

USERS = ("viewer_a", "owner_b", "outsider")
WINDOW = {"date_from": "2026-03-01", "date_to": "2026-05-31"}
HORIZON = (dt.date(2026, 6, 1), dt.date(2026, 6, 14))


def email(name: str) -> str:
    return f"priorities-{name}@example.test"


def occupy(session: Session, hotel: Hotel, room: Room, night: dt.date) -> None:
    guest = make_guest(session, hotel)
    booking = make_booking(
        session, hotel, guest, check_in=night, check_out=night + dt.timedelta(days=1)
    )
    booking.booked_at = dt.datetime.combine(
        night - dt.timedelta(days=30), dt.time(12), tzinfo=dt.UTC
    )
    session.flush()
    price_nights(session, allocate_room(session, booking, room), ["100.00"])


def seed(session: Session, slug: str, rooms: int, busy_weekday: int) -> Hotel:
    """Every `busy_weekday` from March to May fully occupied; every other day empty."""
    hotel = make_hotel(session, slug=slug)
    room_type = make_room_type(session, hotel)
    created = [make_room(session, hotel, room_type, number=f"{101 + n}") for n in range(rooms)]
    day = dt.date(2026, 3, 1)
    while day <= dt.date(2026, 5, 31):
        if day.weekday() == busy_weekday:
            for room in created:
                occupy(session, hotel, room, day)
        day += dt.timedelta(days=1)
    session.commit()
    return hotel


@dataclass
class World:
    session: Session
    a: Hotel
    b: Hotel
    app: Any
    tokens: dict[str, str]
    model: dict[str, ChatModel] = field(default_factory=dict)

    def client(self, name: str) -> TestClient:
        return TestClient(self.app, headers={"Authorization": f"Bearer {self.tokens[name]}"})

    def get(self, name: str, hotel: Hotel | str, **params: Any) -> httpx.Response:
        public_id = hotel.public_id if isinstance(hotel, Hotel) else hotel
        return self.client(name).get(
            f"/api/v1/hotels/{public_id}/intelligence/priorities", params={**WINDOW, **params}
        )


@pytest.fixture
def world(engine: Engine, session: Session) -> World:
    a = seed(session, "priorities-a", rooms=1, busy_weekday=5)  # Saturdays
    b = seed(session, "priorities-b", rooms=3, busy_weekday=0)  # Mondays
    app = create_test_app(engine, auth_login_rate_limit=100)
    bootstrap = TestClient(app)
    tokens = {name: register_and_login(bootstrap, email(name)) for name in USERS}
    grant_membership(engine, email("viewer_a"), str(a.public_id), "viewer")
    grant_membership(engine, email("owner_b"), str(b.public_id), "owner")
    world = World(session, a, b, app, tokens)
    world.model["current"] = ScriptedModel("unused")
    app.dependency_overrides[get_chat_model] = lambda: world.model["current"]
    return world


def peaks(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in body["items"] if item["kind"] == "upcoming_peak_day"]


# ======================================================================================
# The list, from the hotel's own data
# ======================================================================================


def test_a_viewer_gets_the_hotels_attention_list(world: World) -> None:
    response = world.get("viewer_a", world.a)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["hotel_public_id"] == str(world.a.public_id)
    assert body["method"]["evaluation_protocol"] == "insight_ranking_v1"
    assert (body["horizon"]["date_from"], body["horizon"]["date_to"]) == (
        HORIZON[0].isoformat(),
        HORIZON[1].isoformat(),
    )
    assert [item["rank"] for item in body["items"]] == list(range(1, len(body["items"]) + 1))
    # A is busy on Saturdays: the two Saturdays of the horizon lead the peak days.
    saturdays = [
        item["date_from"]
        for item in peaks(body)
        if dt.date.fromisoformat(item["date_from"]).weekday() == 5
    ]
    assert saturdays == ["2026-06-06", "2026-06-13"]
    assert peaks(body)[0]["date_from"] == "2026-06-06"


def test_peak_figures_are_exactly_what_the_forecast_route_serves(world: World) -> None:
    body = world.get("viewer_a", world.a).json()
    forecast = world.client("viewer_a").get(
        f"/api/v1/hotels/{world.a.public_id}/intelligence/forecast/occupancy",
        params={
            "date_from": HORIZON[0].isoformat(),
            "date_to": HORIZON[1].isoformat(),
            "training_days": 90,
        },
    )
    assert forecast.status_code == 200
    by_date = {point["date"]: point for point in forecast.json()["points"]}
    for item in peaks(body):
        source = by_date[item["date_from"]]
        figures = {f["name"]: f["value"] for f in item["figures"]}
        assert figures["predicted_room_nights"] == source["predicted_room_nights"]
        assert figures["on_the_books_room_nights"] == str(source["on_the_books_room_nights"])
        assert figures["available_room_nights"] == str(source["available_room_nights"])
        assert item["look_at"]["path"] == (
            "/api/v1/hotels/{hotel_public_id}/intelligence/forecast/occupancy"
        )


def test_the_same_request_gives_the_same_list(world: World) -> None:
    assert world.get("viewer_a", world.a).json() == world.get("viewer_a", world.a).json()


def test_each_hotel_sees_only_its_own_data(world: World) -> None:
    a = world.get("viewer_a", world.a).json()
    b = world.get("owner_b", world.b).json()
    assert {dt.date.fromisoformat(i["date_from"]).weekday() for i in peaks(a)[:2]} == {5}
    assert dt.date.fromisoformat(peaks(b)[0]["date_from"]).weekday() == 0
    # B's forecast is in three-room units; nothing of it appears in A's list.
    assert all(
        f["value"] != "3"
        for item in peaks(a)
        for f in item["figures"]
        if f["name"] == "available_room_nights"
    )


# ======================================================================================
# Authorization
# ======================================================================================


def test_another_hotels_member_and_a_non_member_get_404(world: World) -> None:
    assert world.get("owner_b", world.a).status_code == 404
    assert world.get("outsider", world.a).status_code == 404
    assert world.get("viewer_a", world.b).status_code == 404
    unknown = world.get("viewer_a", str(uuid.uuid4()))
    assert unknown.status_code == 404
    assert world.get("outsider", world.a).json() == unknown.json()


def test_an_anonymous_caller_is_refused(world: World) -> None:
    response = TestClient(world.app).get(
        f"/api/v1/hotels/{world.a.public_id}/intelligence/priorities", params=WINDOW
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "params",
    [
        {"date_from": "2026-05-31", "date_to": "2026-05-01"},
        {"date_from": "2024-01-01", "date_to": "2026-05-31"},
    ],
    ids=["inverted", "too-long"],
)
def test_a_bad_window_is_a_validation_error(world: World, params: dict[str, str]) -> None:
    assert world.get("viewer_a", world.a, **params).status_code == 422


def test_it_writes_nothing(world: World) -> None:
    def counts() -> dict[str, int]:
        return {
            table: int(world.session.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one())
            for table in ("audit_events", "bookings", "demand_predictions", "llm_invocations")
        }

    before = counts()
    world.get("viewer_a", world.a)
    assert counts() == before


# ======================================================================================
# The seventh tool, through the real copilot
# ======================================================================================


def ask(world: World, model: ScriptedModel) -> dict[str, Any]:
    world.model["current"] = model
    response = world.client("viewer_a").post(
        f"/api/v1/hotels/{world.a.public_id}/copilot/ask",
        json={"question": "What should I watch in June?"},
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_the_copilot_tool_reads_the_path_hotels_list(world: World) -> None:
    call = ToolCall(id="p1", name="get_hotel_priorities", arguments=dict(WINDOW))
    model = ScriptedModel([ScriptedTurn(tool_calls=(call,)), "The list is ready."])

    body = ask(world, model)

    assert body["tools_used"] == [{"tool": "get_hotel_priorities", "outcome": "succeeded"}]
    shown = next(m.content for m in model.calls[-1].messages if m.role == "tool")
    assert "2026-06-06" in shown
    assert str(world.a.public_id) not in shown and str(world.b.public_id) not in shown
    [event] = world.session.execute(
        sa.text(
            "SELECT hotel_id, resource_reference FROM audit_events WHERE action = 'tool.invoked'"
        )
    ).all()
    assert (event.hotel_id, event.resource_reference) == (world.a.id, "get_hotel_priorities")


def test_a_model_supplied_hotel_is_refused_by_the_tool(world: World) -> None:
    call = ToolCall(
        id="p1",
        name="get_hotel_priorities",
        arguments={**WINDOW, "hotel_public_id": str(world.b.public_id)},
    )
    body = ask(world, ScriptedModel([ScriptedTurn(tool_calls=(call,)), "Nothing."]))
    assert body["tools_used"] == [{"tool": "get_hotel_priorities", "outcome": "invalid_arguments"}]
