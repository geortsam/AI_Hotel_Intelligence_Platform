"""Optimistic concurrency on PATCH, against real PostgreSQL (Issue H6).

Five operations take an optional ``If-Match`` carrying the ``updated_at`` the client loaded:
hotel, guest, room type, room and booking. A version that is no longer current is refused with
412 ``STALE_UPDATE`` and **nothing is written**; without the header every update behaves as it
did before H6. The comparison runs under a row lock taken in the update's own transaction, so a
writer that commits while the update waits is seen, not overwritten.

Client A and client B are two members of the same hotel, each with their own session.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.api.preconditions import entity_tag
from tests.integration.conftest import authenticated_client, grant_membership, requires_postgres
from tests.integration.test_reviews_api import build_booking

pytestmark = requires_postgres

A_EMAIL = "concurrency-a@example.test"
B_EMAIL = "concurrency-b@example.test"

#: name -> (url template, client B's edit, client A's edit). The two touch different fields,
#: which is exactly the case last-write-wins used to lose silently.
RESOURCES: dict[str, tuple[str, dict[str, Any], dict[str, Any]]] = {
    "hotel": (
        "/api/v1/hotels/{hotel}",
        {"phone": "+30 210 000 0001"},
        {"name": "Renamed by A"},
    ),
    "guest": (
        "/api/v1/hotels/{hotel}/guests/{guest}",
        {"phone": "+30 690 000 0001"},
        {"notes": "Written by A"},
    ),
    "room-type": (
        "/api/v1/hotels/{hotel}/room-types/DLX",
        {"description": "Written by B"},
        {"name": "Renamed by A"},
    ),
    "room": (
        "/api/v1/hotels/{hotel}/room-types/DLX/rooms/101",
        {"notes": "Written by B"},
        {"floor": 3},
    ),
    "booking": (
        "/api/v1/hotels/{hotel}/bookings/{booking}",
        {"special_requests": "Written by B"},
        {"channel_reference": "A-REF-1"},
    ),
}


# --- setup ----------------------------------------------------------------------------------------


@pytest.fixture
def api(engine: Engine) -> Iterator[TestClient]:
    """Client A. Owner of every hotel it creates."""
    client = authenticated_client(engine, email=A_EMAIL)
    yield client
    with sessionmaker(bind=engine, future=True)() as cleanup:
        cleanup.execute(sa.text("TRUNCATE hotels RESTART IDENTITY CASCADE"))
        cleanup.commit()


def client_b(engine: Engine, hotel: str) -> TestClient:
    """A second member, with their own session, editing the same hotel."""
    client = authenticated_client(engine, email=B_EMAIL)
    grant_membership(engine, B_EMAIL, hotel, "owner")
    return client


def resource_url(api: TestClient, name: str) -> tuple[str, str]:
    """Build a hotel with a room type, a room, a guest and a booking; return (hotel, url)."""
    hotel, booking, guest = build_booking(api, "hotel-a")
    template = RESOURCES[name][0]
    return hotel, template.format(hotel=hotel, guest=guest, booking=booking)


def tag(record: dict[str, Any]) -> str:
    """What a client sends: the ``updated_at`` it received, quoted."""
    return f'"{record["updated_at"]}"'


def assert_stale(response: Response) -> None:
    assert response.status_code == 412, response.text
    body = response.json()
    assert body["error"]["code"] == "STALE_UPDATE"
    assert body["error"]["details"] == []


ALL = pytest.mark.parametrize("name", sorted(RESOURCES))


# --- a current version applies; a stale one writes nothing ----------------------------------------


@ALL
def test_a_current_version_applies_the_update(api: TestClient, name: str) -> None:
    _, url = resource_url(api, name)
    loaded = api.get(url).json()
    edit = RESOURCES[name][2]

    response = api.patch(url, json=edit, headers={"If-Match": tag(loaded)})

    assert response.status_code == 200, response.text
    for field, value in edit.items():
        assert response.json()[field] == value
    assert response.json()["updated_at"] != loaded["updated_at"]


@ALL
def test_a_stale_version_is_refused_and_nothing_is_written(
    api: TestClient, engine: Engine, name: str
) -> None:
    """Client A loads; client B saves; client A saves from what it loaded. Refused, and the
    record is exactly what B left -- every field, ``updated_at`` included."""
    hotel, url = resource_url(api, name)
    b = client_b(engine, hotel)
    _, b_edit, a_edit = RESOURCES[name]

    loaded_by_a = api.get(url).json()
    after_b = b.patch(url, json=b_edit)
    assert after_b.status_code == 200, after_b.text

    refused = api.patch(url, json=a_edit, headers={"If-Match": tag(loaded_by_a)})

    assert_stale(refused)
    assert api.get(url).json() == after_b.json()


@ALL
def test_after_a_refusal_a_reload_and_resubmit_applies(
    api: TestClient, engine: Engine, name: str
) -> None:
    """Nothing is left locked or half-done by a 412: the client reloads and tries again."""
    hotel, url = resource_url(api, name)
    b = client_b(engine, hotel)
    _, b_edit, a_edit = RESOURCES[name]
    stale = api.get(url).json()
    assert b.patch(url, json=b_edit).status_code == 200
    assert_stale(api.patch(url, json=a_edit, headers={"If-Match": tag(stale)}))

    reloaded = api.get(url).json()
    response = api.patch(url, json=a_edit, headers={"If-Match": tag(reloaded)})

    assert response.status_code == 200, response.text
    for field, value in {**b_edit, **a_edit}.items():
        assert response.json()[field] == value


# --- without the header, nothing changed ----------------------------------------------------------


@ALL
def test_without_the_header_an_update_behaves_as_before(
    api: TestClient, engine: Engine, name: str
) -> None:
    """Backward compatible: no precondition, no refusal -- the stale client's write applies,
    and because a PATCH writes only the fields it sends, B's field survives it."""
    hotel, url = resource_url(api, name)
    b = client_b(engine, hotel)
    _, b_edit, a_edit = RESOURCES[name]
    api.get(url)
    assert b.patch(url, json=b_edit).status_code == 200

    response = api.patch(url, json=a_edit)

    assert response.status_code == 200, response.text
    for field, value in {**b_edit, **a_edit}.items():
        assert response.json()[field] == value


@ALL
def test_a_wildcard_constrains_nothing(api: TestClient, engine: Engine, name: str) -> None:
    hotel, url = resource_url(api, name)
    b = client_b(engine, hotel)
    assert b.patch(url, json=RESOURCES[name][1]).status_code == 200

    response = api.patch(url, json=RESOURCES[name][2], headers={"If-Match": "*"})

    assert response.status_code == 200, response.text


# --- malformed preconditions ----------------------------------------------------------------------

MALFORMED = [
    'W/"2026-10-09T10:00:00.123456Z"',
    "2026-10-09T10:00:00.123456Z",
    '"2026-10-09T10:00:00Z", "2026-10-09T11:00:00Z"',
    '"not-a-time"',
    '"2026-10-09T10:00:00.123456"',
    '"2026-10-09T10:00:00.1234567Z"',
]


@ALL
@pytest.mark.parametrize("value", MALFORMED)
def test_a_malformed_precondition_is_a_422_and_writes_nothing(
    api: TestClient, name: str, value: str
) -> None:
    _, url = resource_url(api, name)
    before = api.get(url).json()

    response = api.patch(url, json=RESOURCES[name][2], headers={"If-Match": value})

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert [detail["location"] for detail in error["details"]] == [["header", "If-Match"]]
    assert value not in response.text
    assert api.get(url).json() == before


# --- the token itself -----------------------------------------------------------------------------


@ALL
def test_any_rendering_of_the_same_instant_matches(api: TestClient, name: str) -> None:
    """The canonical UTC form and the API's own string name one instant; both match."""
    _, url = resource_url(api, name)
    loaded = api.get(url).json()
    canonical = entity_tag(dt.datetime.fromisoformat(loaded["updated_at"]))

    response = api.patch(url, json=RESOURCES[name][2], headers={"If-Match": canonical})

    assert response.status_code == 200, response.text


@ALL
def test_one_microsecond_off_is_stale(api: TestClient, name: str) -> None:
    """Full precision, both ways: the token is compared to the microsecond PostgreSQL stored."""
    _, url = resource_url(api, name)
    loaded = api.get(url).json()
    instant = dt.datetime.fromisoformat(loaded["updated_at"])

    for nudged in (instant + dt.timedelta(microseconds=1), instant - dt.timedelta(microseconds=1)):
        assert_stale(
            api.patch(url, json=RESOURCES[name][2], headers={"If-Match": entity_tag(nudged)})
        )
    assert api.get(url).json() == loaded


@ALL
def test_an_empty_update_is_still_checked(api: TestClient, engine: Engine, name: str) -> None:
    """The precondition is judged before the empty-update shortcut: a stale empty PATCH is a
    412, a current one returns the record unchanged and writes nothing."""
    hotel, url = resource_url(api, name)
    b = client_b(engine, hotel)
    stale = api.get(url).json()
    assert b.patch(url, json=RESOURCES[name][1]).status_code == 200
    current = api.get(url).json()

    assert_stale(api.patch(url, json={}, headers={"If-Match": tag(stale)}))
    response = api.patch(url, json={}, headers={"If-Match": tag(current)})

    assert response.status_code == 200, response.text
    assert response.json() == current
    assert api.get(url).json() == current


# --- the comparison runs under the row lock -------------------------------------------------------


def wait_for_a_lock_wait(
    engine: Engine, worker: threading.Thread, outcome: dict[str, Any], *, seconds: float = 15.0
) -> None:
    """Until some backend of this database is waiting on a lock -- the PATCH, blocked.

    Each look is its own transaction: ``pg_stat_activity`` is a snapshot cached for the life of
    the transaction that first reads it, so a single long-lived one would keep seeing the
    moment before the update connected.
    """
    deadline = time.monotonic() + seconds
    with engine.connect() as watcher:
        while time.monotonic() < deadline:
            with watcher.begin():
                waiting = watcher.execute(
                    sa.text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                    )
                ).scalar_one()
            if waiting:
                return
            if not worker.is_alive():
                pytest.fail(f"the update finished without waiting on the row lock: {outcome}")
            time.sleep(0.05)
    pytest.fail("the conditional update never waited on the row lock")


def test_a_writer_that_commits_while_the_update_waits_is_seen_not_overwritten(
    api: TestClient, engine: Engine
) -> None:
    """Another transaction holds the guest's row. Client A's conditional update -- carrying
    the version that is current *at this moment* -- must wait for it, then compare against
    what it committed. Compared before the lock, A would see its own version as current and
    write over the other transaction's change."""
    _, url = resource_url(api, "guest")
    guest = url.rsplit("/", 1)[1]
    token = tag(api.get(url).json())
    outcome: dict[str, Any] = {}

    def submit() -> None:
        try:
            outcome["response"] = api.patch(
                url, json={"notes": "Written by A"}, headers={"If-Match": token}
            )
        except Exception as failure:
            outcome["failure"] = repr(failure)

    with engine.connect() as other:
        transaction = other.begin()
        other.execute(sa.text("SET LOCAL lock_timeout = '20s'"))
        other.execute(
            sa.text("SELECT 1 FROM guests WHERE public_id = CAST(:g AS uuid) FOR UPDATE"),
            {"g": guest},
        )
        worker = threading.Thread(target=submit)
        worker.start()
        wait_for_a_lock_wait(engine, worker, outcome)
        other.execute(
            sa.text(
                "UPDATE guests SET phone = '+30 690 000 0009' WHERE public_id = CAST(:g AS uuid)"
            ),
            {"g": guest},
        )
        transaction.commit()

    worker.join(timeout=30)
    assert not worker.is_alive(), "the conditional update never finished"
    assert_stale(outcome["response"])
    record = api.get(url).json()
    assert record["phone"] == "+30 690 000 0009"
    assert record["notes"] is None
