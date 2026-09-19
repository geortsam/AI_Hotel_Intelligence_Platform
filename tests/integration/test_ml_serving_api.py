"""The demand-model endpoint against real PostgreSQL, where the claims can be attacked.

The pure rules -- feature order, artifact refusals, load-once, no training -- are in
``tests/backend/test_ml_serving.py`` and need no database. What needs one is everything this
file covers, and none of it could be checked honestly against SQLite:

* **Tenant isolation.** Two hotels hold history over the SAME dates in the SAME tables. One
  hotel's prediction must not move when the other's history is enlarged, and a hotel with no
  history of its own must be refused rather than answered from its neighbour's rows.
* **Authorization.** A non-member and an unknown hotel must produce the same 404, byte for
  byte, or the endpoint becomes an existence oracle for the whole estate.
* **Leakage.** A prediction is taken, bookings are then created *inside the horizon* -- on the
  target day itself and on the six days before it -- and the same prediction is taken again. It
  must be identical, while a query proves the writes really happened.
* **Feature acquisition.** One grouped query for a 22-day window, counted at the driver, not
  twenty-two.

**All data here is test fixture data**, created in the disposable database this suite is given
and truncated with it. The Stage 5.17 guard refuses an unsafe target before a statement runs.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.ml import artifact_store
from app.ml.artifact_store import ArtifactLocation
from app.ml.serving import APPROVED_MODEL, build_feature_values
from app.models.hotel import Hotel
from app.models.room import Room, RoomType
from tests.artifact_builder import build_artifact, rewrite
from tests.integration.conftest import (
    allocate_room,
    authenticated_client,
    create_test_app,
    grant_membership,
    make_booking,
    make_guest,
    make_hotel,
    make_room,
    make_room_type,
    price_nights,
    requires_postgres,
)

pytestmark = requires_postgres

SUITE_EMAIL = "ml-serving@example.test"
OTHER_EMAIL = "ml-serving-other@example.test"

#: A fixed anchor, so every date below is arithmetic rather than "whatever today is".
TARGET = dt.date(2026, 6, 1)
#: The window the model's three lags reach across: 28 days back, ending at the cutoff.
FIRST_NIGHT = TARGET - dt.timedelta(days=28)
LAST_NIGHT = TARGET - dt.timedelta(days=7)
NIGHTS = (LAST_NIGHT - FIRST_NIGHT).days + 1

#: Two occupancy levels the model scores DIFFERENTLY, which is what lets a leak be detected.
#:
#: Not an arbitrary pair. This estimator bins its features from a training set whose daily
#: demand runs to the hundreds, so everything from 1 to roughly 40 room nights a night falls in
#: one bin and scores identically -- measured, and pinned in
#: ``tests/backend/test_ml_serving.py::test_the_model_cannot_tell_small_hotels_apart``. A suite
#: seeded at 5 against 40 would therefore compare two equal numbers and prove nothing, which is
#: why the isolation test below asserts the two levels differ before it asserts anything else.
QUIET_ROOMS = 5
BUSY_ROOMS = 60


# --- the artifact -------------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def serving_artifact(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ArtifactLocation]:
    """One real artifact for the module, built rather than read from the working tree.

    Module-scoped and NOT reconfigured per test, so ``load_count`` means what it says: the
    number of times this process has actually read and deserialised an artifact.
    """
    location = build_artifact(tmp_path_factory.mktemp("serving-artifact"))
    artifact_store.configure(location)
    yield location
    artifact_store.configure(None)


# --- seeding ------------------------------------------------------------------------------------


def occupy(
    session: Session,
    hotel: Hotel,
    room: Room,
    *,
    first: dt.date,
    nights: int,
) -> None:
    """One complete booking occupying *room* for *nights* consecutive nights.

    One booking per room rather than one per night: the night rows are what the target counts,
    and a booking that covers a whole span produces them in a single insert batch. Complete on
    purpose -- migration 0004's deferred trigger refuses a booking whose night set has a hole,
    so a gap in a hotel's history is made by leaving a gap BETWEEN bookings.
    """
    guest = make_guest(session, hotel)
    booking = make_booking(
        session,
        hotel,
        guest,
        check_in=first,
        check_out=first + dt.timedelta(days=nights),
        status="checked_out",
    )
    # Long before the cutoff, so nothing here depends on when the fixture happened to run.
    booking.booked_at = dt.datetime.combine(
        first - dt.timedelta(days=90), dt.time(12), tzinfo=dt.UTC
    )
    session.flush()
    allocation = allocate_room(session, booking, room)
    price_nights(session, allocation, ["100.00"] * nights)


def seed_hotel(
    session: Session,
    *,
    slug: str,
    rooms: int,
    spans: list[tuple[dt.date, int]] | None = None,
) -> Hotel:
    """A hotel with *rooms* rooms occupied across every span, committed.

    Committed rather than flushed: the API under test reads through its own session, and a
    write that is only flushed is invisible to it.
    """
    hotel = make_hotel(session, slug=slug)
    room_type = make_room_type(session, hotel)
    for index in range(rooms):
        room = make_room(session, hotel, room_type, number=f"{101 + index}")
        for first, nights in spans or [(FIRST_NIGHT, NIGHTS)]:
            occupy(session, hotel, room, first=first, nights=nights)
    session.commit()
    return hotel


def member_client(engine: Engine, hotel: Hotel, *, email: str, role: str = "owner") -> TestClient:
    client = authenticated_client(engine, email=email)
    grant_membership(engine, email, str(hotel.public_id), role)
    return client


def url(hotel: Hotel | uuid.UUID) -> str:
    public_id = hotel.public_id if isinstance(hotel, Hotel) else hotel
    return f"/api/v1/hotels/{public_id}/ml/demand-forecast"


def params(**overrides: object) -> dict[str, object]:
    return {"target_date": str(TARGET), **overrides}


def expected_prediction(level: int) -> float:
    """What the approved model returns for a flat history at *level* rooms per night.

    Computed here through the same artifact the API holds, from features built by hand. It is
    the independent half of the isolation claim: the API's number must equal the number this
    hotel's OWN history produces, not merely differ from its neighbour's.
    """
    history = {TARGET - dt.timedelta(days=offset): level for offset in range(7, 7 + NIGHTS)}
    return artifact_store.predict_room_nights(
        artifact_store.approved_model(),
        hotel_public_id=uuid.uuid4(),
        target_date=TARGET,
        features=build_feature_values(history, TARGET),
    )


# --- fixtures -----------------------------------------------------------------------------------


@pytest.fixture
def quiet(session: Session) -> Hotel:
    return seed_hotel(session, slug="ml-quiet", rooms=QUIET_ROOMS)


@pytest.fixture
def api(engine: Engine, quiet: Hotel) -> TestClient:
    return member_client(engine, quiet, email=SUITE_EMAIL)


# --- the happy path -----------------------------------------------------------------------------


def test_a_member_gets_a_prediction_with_its_full_provenance(api: TestClient, quiet: Hotel) -> None:
    response = api.get(url(quiet), params=params())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["hotel_public_id"] == str(quiet.public_id)
    assert body["target_date"] == str(TARGET)
    assert body["forecast_horizon_days"] == 7
    assert body["cutoff_date"] == str(LAST_NIGHT)
    assert body["prediction_cutoff"] == "2026-05-26T00:00:00Z"
    assert isinstance(body["predicted_room_nights"], float)
    assert body["features_used"] == list(APPROVED_MODEL.feature_columns)
    assert body["model"] == {
        "model_name": "demand_baseline",
        "model_version": "demand_baseline_v1",
        "feature_version": "v1",
        "dataset_version": "v1",
        "status": "offline_research_candidate",
        "production_ready": False,
        "methodology": body["model"]["methodology"],
    }
    assert "gradient" in body["model"]["methodology"].lower()


def test_the_number_is_the_one_this_hotels_own_history_produces(
    api: TestClient, quiet: Hotel
) -> None:
    """Not "a number": the number. Computed independently from the seeded level."""
    body = api.get(url(quiet), params=params()).json()

    assert body["predicted_room_nights"] == expected_prediction(QUIET_ROOMS)


def test_the_response_carries_no_internal_identifier(api: TestClient, quiet: Hotel) -> None:
    response = api.get(url(quiet), params=params())
    body = response.json()

    assert set(body) == {
        "hotel_public_id",
        "target_date",
        "forecast_horizon_days",
        "cutoff_date",
        "prediction_cutoff",
        "predicted_room_nights",
        "model",
        "features_used",
    }
    assert [key for key in body if key.endswith("_id")] == ["hotel_public_id"]
    assert body["hotel_public_id"] == str(quiet.public_id)
    for banned in ("hotel_id", "guest_id", "booking_id", "room_id", '"id"'):
        assert banned not in response.text, banned


def test_a_viewer_may_read_the_forecast(engine: Engine, quiet: Hotel) -> None:
    """The lowest role is enough. This is a read of the hotel's own operational data, on the
    same footing as the analytics and intelligence routes beside it."""
    viewer = member_client(engine, quiet, email=OTHER_EMAIL, role="viewer")

    assert viewer.get(url(quiet), params=params()).status_code == 200


# --- authorization and tenant isolation -----------------------------------------------------------


def test_an_unauthenticated_request_is_refused(engine: Engine, quiet: Hotel) -> None:
    anonymous = TestClient(create_test_app(engine))

    response = anonymous.get(url(quiet), params=params())

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_TOKEN"


def test_a_non_member_and_an_unknown_hotel_are_indistinguishable(
    engine: Engine, quiet: Hotel
) -> None:
    """A 403 for a non-member would confirm the property exists, which is the one thing hotel
    isolation may never do."""
    stranger = authenticated_client(engine, email=OTHER_EMAIL)

    refused = stranger.get(url(quiet), params=params())
    unknown = stranger.get(url(uuid.uuid4()), params=params())

    assert refused.status_code == unknown.status_code == 404
    assert refused.json() == unknown.json()
    assert refused.json()["error"]["message"] == "Hotel not found."


def test_a_member_of_one_hotel_cannot_reach_another(
    engine: Engine, session: Session, quiet: Hotel
) -> None:
    busy = seed_hotel(session, slug="ml-busy", rooms=BUSY_ROOMS)
    client = member_client(engine, quiet, email=SUITE_EMAIL)

    assert client.get(url(quiet), params=params()).status_code == 200
    assert client.get(url(busy), params=params()).status_code == 404


def test_a_hotel_with_no_history_is_refused_while_its_neighbour_has_plenty(
    engine: Engine, session: Session
) -> None:
    """The sharpest isolation test available: the two hotels share the tables and the dates,
    and one of them must still be unanswerable."""
    seed_hotel(session, slug="ml-neighbour", rooms=BUSY_ROOMS)
    empty = make_hotel(session, slug="ml-empty")
    make_room_type(session, empty)
    session.commit()
    client = member_client(engine, empty, email=SUITE_EMAIL)

    response = client.get(url(empty), params=params())

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INSUFFICIENT_HISTORY"


def test_one_hotels_history_never_reaches_anothers_prediction(
    engine: Engine, session: Session, quiet: Hotel
) -> None:
    """Each hotel's number is the one its own history produces, and neither is the other's."""
    busy = seed_hotel(session, slug="ml-busy", rooms=BUSY_ROOMS)
    client = member_client(engine, quiet, email=SUITE_EMAIL)
    grant_membership(engine, SUITE_EMAIL, str(busy.public_id), "owner")

    quiet_value = client.get(url(quiet), params=params()).json()["predicted_room_nights"]
    busy_value = client.get(url(busy), params=params()).json()["predicted_room_nights"]

    assert expected_prediction(QUIET_ROOMS) != expected_prediction(BUSY_ROOMS), (
        "the two seeded levels score identically, so this test could not detect a leak"
    )
    assert quiet_value == expected_prediction(QUIET_ROOMS)
    assert busy_value == expected_prediction(BUSY_ROOMS)


def test_enlarging_one_hotel_does_not_move_anothers_prediction(
    engine: Engine, session: Session, quiet: Hotel
) -> None:
    before_neighbour = member_client(engine, quiet, email=SUITE_EMAIL)
    before = before_neighbour.get(url(quiet), params=params()).content

    busy = seed_hotel(session, slug="ml-loud", rooms=BUSY_ROOMS)
    after = before_neighbour.get(url(quiet), params=params()).content

    written = session.execute(
        sa.text("SELECT count(*) FROM booking_room_nights WHERE hotel_id = :hotel"),
        {"hotel": busy.id},
    ).scalar_one()

    assert written > 0, "the neighbour's history was never written, so this proves nothing"
    assert after == before


# --- leakage --------------------------------------------------------------------------------------


def test_bookings_inside_the_horizon_cannot_change_the_prediction(
    api: TestClient, session: Session, quiet: Hotel
) -> None:
    """Writes on the target day and on every day between the cutoff and it. None may be read."""
    before = api.get(url(quiet), params=params()).content

    room_type = session.execute(
        sa.select(RoomType).where(RoomType.hotel_id == quiet.id)
    ).scalar_one()
    for index in range(3):
        room = make_room(session, quiet, room_type, number=f"9{index:02d}")
        occupy(session, quiet, room, first=LAST_NIGHT + dt.timedelta(days=1), nights=7)
    session.commit()

    written = session.execute(
        sa.text(
            "SELECT count(*) FROM booking_room_nights "
            "WHERE hotel_id = :hotel AND stay_date > :cutoff"
        ),
        {"hotel": quiet.id, "cutoff": LAST_NIGHT},
    ).scalar_one()

    assert written == 21, "the horizon was not populated, so this proves nothing"
    assert api.get(url(quiet), params=params()).content == before


def test_the_cutoff_is_seven_days_before_the_target_and_is_utc(
    api: TestClient, quiet: Hotel
) -> None:
    body = api.get(url(quiet), params=params()).json()

    cutoff = dt.datetime.fromisoformat(body["prediction_cutoff"])
    assert cutoff.utcoffset() == dt.timedelta(0)
    assert dt.date.fromisoformat(body["cutoff_date"]) == TARGET - dt.timedelta(days=7)
    assert cutoff.date() == TARGET - dt.timedelta(days=6)


# --- determinism and concurrency ------------------------------------------------------------------


def test_repeated_requests_are_byte_identical(api: TestClient, quiet: Hotel) -> None:
    bodies = {api.get(url(quiet), params=params()).content for _ in range(6)}

    assert len(bodies) == 1


def test_concurrent_requests_agree_and_load_the_artifact_once(
    engine: Engine, api: TestClient, quiet: Hotel
) -> None:
    """A synchronous endpoint runs in FastAPI's threadpool, so concurrency here is real.

    Each thread gets its own client over its own application instance, which is the harsher
    arrangement: the artifact cache is process-wide, so one load must still serve all of them.
    """
    token = api.headers["Authorization"]
    artifact_store.reset()

    def ask(_: int) -> bytes:
        client = TestClient(create_test_app(engine), headers={"Authorization": token})
        return client.get(url(quiet), params=params()).content

    with ThreadPoolExecutor(max_workers=8) as pool:
        bodies = set(pool.map(ask, range(8)))

    assert len(bodies) == 1
    assert artifact_store.load_count() == 1


def test_the_artifact_is_not_reloaded_per_request(api: TestClient, quiet: Hotel) -> None:
    artifact_store.reset()

    for _ in range(10):
        assert api.get(url(quiet), params=params()).status_code == 200

    assert artifact_store.load_count() == 1


def test_a_request_never_trains(
    api: TestClient, quiet: Hotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sklearn.ensemble import HistGradientBoostingRegressor

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("a request called a training method")

    for name in ("fit", "fit_predict", "partial_fit"):
        if hasattr(HistGradientBoostingRegressor, name):
            monkeypatch.setattr(HistGradientBoostingRegressor, name, explode, raising=False)

    assert api.get(url(quiet), params=params()).status_code == 200


# --- feature acquisition --------------------------------------------------------------------------


def test_feature_acquisition_is_one_query_for_the_whole_window(
    engine: Engine, api: TestClient, quiet: Hotel
) -> None:
    """Twenty-two days, one grouped scan. Three single-date lookups would be the shape that
    becomes N, and a per-date correlated count would be N already."""
    statements: list[str] = []

    def record(conn: object, cursor: object, statement: str, *rest: object) -> None:
        statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        assert api.get(url(quiet), params=params()).status_code == 200
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)

    night_queries = [text for text in statements if "booking_room_nights" in text]
    assert len(night_queries) == 1, night_queries
    assert len(statements) <= 6, statements


def test_a_gap_in_the_history_is_refused_rather_than_filled(
    engine: Engine, session: Session
) -> None:
    """The lag at fourteen days is missing; nothing invents a value for it."""
    gap_day = TARGET - dt.timedelta(days=14)
    hotel = seed_hotel(
        session,
        slug="ml-gap",
        rooms=QUIET_ROOMS,
        spans=[
            (FIRST_NIGHT, (gap_day - FIRST_NIGHT).days),
            (gap_day + dt.timedelta(days=1), (LAST_NIGHT - gap_day).days),
        ],
    )
    client = member_client(engine, hotel, email=SUITE_EMAIL)

    absent = session.execute(
        sa.text(
            "SELECT count(*) FROM booking_room_nights WHERE hotel_id = :hotel AND stay_date = :day"
        ),
        {"hotel": hotel.id, "day": gap_day},
    ).scalar_one()
    response = client.get(url(hotel), params=params())

    assert absent == 0, "the gap was not created, so this proves nothing"
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INSUFFICIENT_HISTORY"


# --- the request contract -------------------------------------------------------------------------


@pytest.mark.parametrize("horizon", [1, 6, 8, 14, 90])
def test_a_horizon_this_model_does_not_forecast_is_refused(
    api: TestClient, quiet: Hotel, horizon: int
) -> None:
    response = api.get(url(quiet), params=params(horizon_days=horizon))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("horizon", [0, -1, 91, "seven"])
def test_a_horizon_outside_the_bounds_never_reaches_the_service(
    api: TestClient, quiet: Hotel, horizon: object
) -> None:
    response = api.get(url(quiet), params=params(horizon_days=horizon))

    assert response.status_code == 422


def test_the_served_horizon_is_accepted_explicitly(api: TestClient, quiet: Hotel) -> None:
    stated = api.get(url(quiet), params=params(horizon_days=7))
    implied = api.get(url(quiet), params=params())

    assert stated.status_code == 200
    assert stated.content == implied.content


@pytest.mark.parametrize("value", ["not-a-date", "2026-13-01", "", "2026-06-01T12:00:00"])
def test_a_target_date_that_is_not_a_date_is_refused(
    api: TestClient, quiet: Hotel, value: str
) -> None:
    response = api.get(url(quiet), params={"target_date": value})

    assert response.status_code == 422


def test_the_target_date_is_required(api: TestClient, quiet: Hotel) -> None:
    assert api.get(url(quiet)).status_code == 422


@pytest.mark.parametrize(
    "extra", [{"artifact_path": "/etc/passwd"}, {"model_version": "v9"}, {"hotel_id": "1"}]
)
def test_an_unknown_query_parameter_changes_nothing(
    api: TestClient, quiet: Hotel, extra: dict[str, str]
) -> None:
    """FastAPI ignores unknown query parameters, so the claim worth asserting is that ignoring
    them is harmless: the response is the one the legitimate parameters alone produce."""
    baseline = api.get(url(quiet), params=params()).content

    assert api.get(url(quiet), params=params(**extra)).content == baseline


# --- the model being unavailable ------------------------------------------------------------------


@pytest.fixture
def restore_artifact(serving_artifact: ArtifactLocation) -> Iterator[ArtifactLocation]:
    yield serving_artifact
    artifact_store.configure(serving_artifact)


def test_an_absent_artifact_is_a_503_that_describes_nothing(
    api: TestClient,
    quiet: Hotel,
    restore_artifact: ArtifactLocation,
    tmp_path: Path,
) -> None:
    artifact_store.configure(
        ArtifactLocation(payload=tmp_path / "gone.pkl", metadata=restore_artifact.metadata)
    )

    response = api.get(url(quiet), params=params())

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "MODEL_UNAVAILABLE"
    for leak in ("pkl", "artifact.json", "sklearn", "pickle", str(tmp_path)):
        assert leak not in error["message"]


def test_an_artifact_that_is_not_the_approved_model_is_the_same_503(
    api: TestClient,
    quiet: Hotel,
    restore_artifact: ArtifactLocation,
    tmp_path: Path,
) -> None:
    """A swapped model and a missing one look identical from outside. Which it was is an
    operator's question, and it is answered in the log rather than in the response."""
    artifact_store.configure(
        rewrite(restore_artifact, tmp_path, model__model_version="demand_baseline_v2")
    )

    response = api.get(url(quiet), params=params())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "MODEL_UNAVAILABLE"


def test_the_model_being_unavailable_does_not_leak_the_hotels_existence(
    engine: Engine,
    quiet: Hotel,
    restore_artifact: ArtifactLocation,
    tmp_path: Path,
) -> None:
    """Authorization still runs first. A stranger gets 404, not 503 -- otherwise the error code
    alone would answer "does this hotel exist?" whenever the model happened to be down."""
    artifact_store.configure(
        ArtifactLocation(payload=tmp_path / "gone.pkl", metadata=restore_artifact.metadata)
    )
    stranger = authenticated_client(engine, email=OTHER_EMAIL)

    assert stranger.get(url(quiet), params=params()).status_code == 404


# --- error hygiene --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "request_params",
    [{"target_date": str(TARGET), "horizon_days": 14}, {"target_date": "nonsense"}, {}],
)
def test_no_error_body_leaks_sql_a_driver_or_a_path(
    api: TestClient, quiet: Hotel, request_params: dict[str, object]
) -> None:
    body = api.get(url(quiet), params=request_params).text.lower()

    for leak in (
        "select ",
        "psycopg",
        "sqlalchemy",
        "sklearn",
        "traceback",
        "booking_room_nights",
        "/usr/",
        ".pkl",
        "artifact",
        "23505",
    ):
        assert leak not in body, leak
