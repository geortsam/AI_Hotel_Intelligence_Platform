"""Stage 6.8 against real PostgreSQL: the row, its identity, and the races.

The pure rules -- the digest, the table shape, the outcome mapping -- are in
``tests/backend/test_prediction_persistence.py`` and need no database. What needs one is
everything here, and none of it could be checked honestly against SQLite or a mock:

* **the unique constraint actually enforcing**, including against a direct INSERT that never
  goes near the service;
* **concurrency actually racing** -- eight simultaneous identical requests, one row;
* **refusals actually writing nothing**, counted in the table rather than inferred from code;
* **tenant isolation**, with two hotels holding predictions over the same dates in one table.

All data here is test fixture data, created in the disposable database this suite is given and
truncated with it. The Stage 5.17 guard refuses an unsafe target before a statement runs.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ml import artifact_store
from app.ml.artifact_store import ArtifactLocation
from app.ml.serving import APPROVED_MODEL, feature_digest
from app.models.hotel import Hotel
from app.models.room import Room, RoomType
from app.repositories.ml_prediction import MlPredictionRepository
from tests.artifact_builder import build_artifact
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

SUITE_EMAIL = "prediction-persistence@example.test"
OTHER_EMAIL = "prediction-persistence-other@example.test"

CANONICAL_DIGEST = "436bf6b3cc5f1a2cc1a2e7fa5e971cedcd0405aa5b7f07a84967b293118a8f70"

TARGET = dt.date(2026, 6, 1)
#: The three days the model's lags read. Seeding only these is the minimum that yields a
#: prediction, and it makes "change one input" a one-booking operation.
LAG_DAYS = (7, 14, 28)


# --- the artifact -----------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def serving_artifact(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ArtifactLocation]:
    location = build_artifact(tmp_path_factory.mktemp("persistence-artifact"))
    artifact_store.configure(location)
    yield location
    artifact_store.configure(None)


# --- seeding ------------------------------------------------------------------------------------


def occupy(session: Session, hotel: Hotel, room: Room, night: dt.date) -> None:
    """One complete one-night booking, which is one room night of realised demand."""
    guest = make_guest(session, hotel)
    booking = make_booking(
        session, hotel, guest, check_in=night, check_out=night + dt.timedelta(days=1)
    )
    booking.booked_at = dt.datetime.combine(
        night - dt.timedelta(days=90), dt.time(12), tzinfo=dt.UTC
    )
    session.flush()
    price_nights(session, allocate_room(session, booking, room), ["100.00"])


def seed_hotel(session: Session, *, slug: str, rooms_per_lag: int = 1) -> Hotel:
    """A hotel with occupancy on exactly the three days the model's lags read."""
    hotel = make_hotel(session, slug=slug)
    room_type = make_room_type(session, hotel)
    for index in range(rooms_per_lag):
        room = make_room(session, hotel, room_type, number=f"{101 + index}")
        for lag in LAG_DAYS:
            occupy(session, hotel, room, TARGET - dt.timedelta(days=lag))
    session.commit()
    return hotel


def member_client(engine: Engine, hotel: Hotel, *, email: str = SUITE_EMAIL) -> TestClient:
    client = authenticated_client(engine, email=email)
    grant_membership(engine, email, str(hotel.public_id), "owner")
    return client


def url(hotel: Hotel | uuid.UUID) -> str:
    public_id = hotel.public_id if isinstance(hotel, Hotel) else hotel
    return f"/api/v1/hotels/{public_id}/ml/demand-forecast"


def params(**overrides: object) -> dict[str, object]:
    return {"target_date": str(TARGET), **overrides}


class RecordingHandler(logging.Handler):
    """Collects records instead of formatting them."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def events() -> Iterator[RecordingHandler]:
    """Everything `app.services.ml_serving` logs, captured at the source.

    Not caplog: it listens on root, and `configure_logging` replaces root's handlers the first
    time any test starts an application. This attaches to the module's own logger, so neither
    root's handlers nor propagation can make it miss a record.
    """
    handler = RecordingHandler()
    logger = logging.getLogger("app.services.ml_serving")
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def field(record: logging.LogRecord, name: str) -> Any:
    """One of the structured fields this stage attaches through ``extra``.

    Read from ``__dict__`` rather than as an attribute: they are added at emit time, so neither
    mypy nor a linter can know they are there, and spelling that out once is better than an
    ignore comment on every assertion.
    """
    return record.__dict__[name]


def rows(session: Session, hotel: Hotel | None = None) -> list[sa.RowMapping]:
    statement = sa.text(
        "SELECT * FROM demand_predictions"
        + (" WHERE hotel_id = :hotel" if hotel else "")
        + " ORDER BY id"
    )
    return list(session.execute(statement, {"hotel": hotel.id} if hotel else {}).mappings())


def count(session: Session) -> int:
    return int(session.execute(sa.text("SELECT count(*) FROM demand_predictions")).scalar_one())


@pytest.fixture
def hotel(session: Session) -> Hotel:
    return seed_hotel(session, slug="pp-hotel")


@pytest.fixture
def api(engine: Engine, hotel: Hotel) -> TestClient:
    return member_client(engine, hotel)


# --- the row --------------------------------------------------------------------------------------


def test_a_served_prediction_writes_exactly_one_row(
    api: TestClient, session: Session, hotel: Hotel
) -> None:
    assert count(session) == 0

    body = api.get(url(hotel), params=params()).json()

    written = rows(session, hotel)
    assert len(written) == 1
    assert written[0]["predicted_room_nights"] == body["predicted_room_nights"]


def test_every_stored_field_matches_the_response_and_the_approved_model(
    api: TestClient, session: Session, hotel: Hotel
) -> None:
    body = api.get(url(hotel), params=params()).json()
    row = rows(session, hotel)[0]

    assert row["hotel_id"] == hotel.id
    assert row["target_date"] == TARGET
    assert row["forecast_horizon_days"] == body["forecast_horizon_days"] == 7
    assert row["prediction_cutoff"] == dt.datetime.fromisoformat(body["prediction_cutoff"])
    assert row["model_name"] == body["model"]["model_name"]
    assert row["model_version"] == body["model"]["model_version"]
    assert row["feature_version"] == body["model"]["feature_version"]
    assert row["dataset_version"] == body["model"]["dataset_version"]
    assert row["canonical_model_digest"] == CANONICAL_DIGEST
    assert row["request_id"] is not None


def test_the_stored_inputs_reproduce_the_stored_digest(
    api: TestClient, session: Session, hotel: Hotel
) -> None:
    """The row can be checked by hand: recompute the digest from what was stored beside it."""
    api.get(url(hotel), params=params())
    row = rows(session, hotel)[0]
    values = row["feature_values"]

    assert set(values) == set(APPROVED_MODEL.feature_columns)
    ordered = {name: values[name] for name in APPROVED_MODEL.feature_columns}
    assert feature_digest(ordered) == row["feature_digest"]
    assert values["demand_lag_7"] == 1.0


# --- idempotency ---------------------------------------------------------------------------------


def test_a_repeated_request_writes_no_second_row_and_moves_nothing(
    api: TestClient, session: Session, hotel: Hotel
) -> None:
    api.get(url(hotel), params=params())
    before = dict(rows(session, hotel)[0])

    for _ in range(4):
        api.get(url(hotel), params=params())

    after = rows(session, hotel)
    assert len(after) == 1
    assert dict(after[0]) == before, "a repeat rewrote the row"
    assert after[0]["generated_at"] == before["generated_at"]


def test_a_changed_input_writes_a_second_row_and_leaves_the_first(
    api: TestClient, session: Session, hotel: Hotel
) -> None:
    """A booking recorded late changes demand_lag_7. That is a new prediction, not a correction.

    The predicted VALUE is deliberately not asserted to differ: this model bins its inputs and
    Stage 6.6 measured that it cannot tell small hotels apart, so two adjacent levels may score
    identically. What must differ is the identity -- different inputs, different row.
    """
    api.get(url(hotel), params=params())
    first = dict(rows(session, hotel)[0])

    # One more room night on target-7, which is exactly one of the three days a lag reads.
    extra = make_room(session, hotel, _room_type_of(session, hotel), number="901")
    occupy(session, hotel, extra, TARGET - dt.timedelta(days=LAG_DAYS[0]))
    session.commit()

    api.get(url(hotel), params=params())

    after = rows(session, hotel)
    assert len(after) == 2, "a changed input did not produce a new prediction"
    assert dict(after[0]) == first, "the earlier prediction was overwritten"
    assert after[0]["feature_digest"] != after[1]["feature_digest"]
    assert after[1]["feature_values"]["demand_lag_7"] == 2.0


def _room_type_of(session: Session, hotel: Hotel) -> RoomType:
    return session.execute(sa.select(RoomType).where(RoomType.hotel_id == hotel.id)).scalar_one()


def test_the_database_rejects_a_duplicate_identity_without_the_service(
    api: TestClient, session: Session, hotel: Hotel
) -> None:
    """The uniqueness rule is the database's, provable without going through any Python."""
    api.get(url(hotel), params=params())
    row = rows(session, hotel)[0]

    with pytest.raises(IntegrityError):
        session.execute(
            sa.text(
                "INSERT INTO demand_predictions (hotel_id, target_date, forecast_horizon_days,"
                " prediction_cutoff, predicted_room_nights, model_name, model_version,"
                " feature_version, dataset_version, canonical_model_digest, feature_values,"
                " feature_digest)"
                " VALUES (:hotel, :target, :horizon, :cutoff, 1.0, :name, :version, 'v1', 'v1',"
                " :digest, '{}'::jsonb, :feature_digest)"
            ),
            {
                "hotel": row["hotel_id"],
                "target": row["target_date"],
                "horizon": row["forecast_horizon_days"],
                "cutoff": row["prediction_cutoff"],
                "name": row["model_name"],
                "version": row["model_version"],
                "digest": row["canonical_model_digest"],
                "feature_digest": row["feature_digest"],
            },
        )
    session.rollback()


# --- refusals write nothing ----------------------------------------------------------------------


def test_an_unauthenticated_request_writes_nothing(
    engine: Engine, session: Session, hotel: Hotel
) -> None:
    anonymous = TestClient(create_test_app(engine))

    assert anonymous.get(url(hotel), params=params()).status_code == 401
    assert count(session) == 0


def test_an_unknown_hotel_writes_nothing(api: TestClient, session: Session) -> None:
    assert api.get(url(uuid.uuid4()), params=params()).status_code == 404
    assert count(session) == 0


def test_a_non_member_writes_nothing(engine: Engine, session: Session, hotel: Hotel) -> None:
    stranger = authenticated_client(engine, email=OTHER_EMAIL)

    assert stranger.get(url(hotel), params=params()).status_code == 404
    assert count(session) == 0


def test_an_invalid_horizon_writes_nothing(api: TestClient, session: Session, hotel: Hotel) -> None:
    assert api.get(url(hotel), params=params(horizon_days=14)).status_code == 422
    assert count(session) == 0


def test_insufficient_history_writes_nothing(engine: Engine, session: Session) -> None:
    empty = make_hotel(session, slug="pp-empty")
    make_room_type(session, empty)
    session.commit()
    client = member_client(engine, empty)

    assert client.get(url(empty), params=params()).status_code == 422
    assert count(session) == 0


def test_an_unavailable_model_writes_nothing(
    api: TestClient,
    session: Session,
    hotel: Hotel,
    serving_artifact: ArtifactLocation,
    tmp_path: Path,
) -> None:
    artifact_store.configure(
        ArtifactLocation(payload=tmp_path / "gone.pkl", metadata=serving_artifact.metadata)
    )
    try:
        assert api.get(url(hotel), params=params()).status_code == 503
        assert count(session) == 0
    finally:
        artifact_store.configure(serving_artifact)


def test_a_persistence_failure_returns_no_prediction_and_no_partial_row(
    api: TestClient, session: Session, hotel: Hotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A served-but-unrecorded prediction would make the table's completeness unverifiable."""

    def explode(self: object, **row: object) -> bool:
        raise IntegrityError("INSERT", {}, Exception("forced"))

    monkeypatch.setattr(MlPredictionRepository, "record", explode)

    response = api.get(url(hotel), params=params())

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "predicted_room_nights" not in response.text
    assert count(session) == 0
    for leak in ("INSERT", "demand_predictions", "constraint", "sqlalchemy", "psycopg"):
        assert leak not in response.text, leak


# --- tenant isolation -----------------------------------------------------------------------------


def test_two_hotels_with_identical_inputs_get_two_rows(
    engine: Engine, session: Session, hotel: Hotel
) -> None:
    twin = seed_hotel(session, slug="pp-twin")
    client = member_client(engine, hotel)
    grant_membership(engine, SUITE_EMAIL, str(twin.public_id), "owner")

    client.get(url(hotel), params=params())
    client.get(url(twin), params=params())

    all_rows = rows(session)
    assert len(all_rows) == 2
    assert {row["hotel_id"] for row in all_rows} == {hotel.id, twin.id}
    # Identical inputs, identical predictions -- and still two rows, because hotel_id is in the
    # identity. A shared row would be one tenant reading another's record.
    assert all_rows[0]["feature_digest"] == all_rows[1]["feature_digest"]


def test_one_hotels_predictions_are_not_reachable_through_another(
    engine: Engine, session: Session, hotel: Hotel
) -> None:
    other = seed_hotel(session, slug="pp-other")
    client = member_client(engine, hotel)
    client.get(url(hotel), params=params())

    assert len(rows(session, hotel)) == 1
    assert rows(session, other) == []
    # And the member of one cannot cause a write against the other.
    assert client.get(url(other), params=params()).status_code == 404
    assert rows(session, other) == []


def test_no_internal_identifier_reaches_the_response(
    api: TestClient, session: Session, hotel: Hotel
) -> None:
    """The row carries internal keys; the response carries none of them, and no persistence
    metadata either -- the contract is exactly what Stage 6.6 published."""
    response = api.get(url(hotel), params=params())
    row = rows(session, hotel)[0]

    assert row["id"] > 0 and row["hotel_id"] == hotel.id
    assert set(response.json()) == {
        "hotel_public_id",
        "target_date",
        "forecast_horizon_days",
        "cutoff_date",
        "prediction_cutoff",
        "predicted_room_nights",
        "model",
        "features_used",
    }
    for banned in ("hotel_id", "feature_digest", "generated_at", "prediction_id", "request_id"):
        assert banned not in response.text, banned


# --- determinism and concurrency ------------------------------------------------------------------


def test_repeated_requests_are_byte_identical_and_resolve_to_one_row(
    api: TestClient, session: Session, hotel: Hotel
) -> None:
    bodies = {api.get(url(hotel), params=params()).content for _ in range(5)}

    assert len(bodies) == 1
    assert len(rows(session, hotel)) == 1


def test_eight_concurrent_identical_requests_produce_one_row(
    engine: Engine, session: Session, hotel: Hotel
) -> None:
    """The race the unique constraint exists for. Not a SELECT-then-INSERT window in sight."""
    client = member_client(engine, hotel)
    token = client.headers["Authorization"]

    def ask(_: int) -> bytes:
        worker = TestClient(create_test_app(engine), headers={"Authorization": token})
        return worker.get(url(hotel), params=params()).content

    with ThreadPoolExecutor(max_workers=8) as pool:
        bodies = set(pool.map(ask, range(8)))

    assert len(bodies) == 1
    assert len(rows(session, hotel)) == 1


def test_a_served_request_emits_exactly_one_event(
    api: TestClient, hotel: Hotel, events: RecordingHandler
) -> None:
    api.get(url(hotel), params=params())

    emitted = [record for record in events.records if hasattr(record, "outcome")]
    assert len(emitted) == 1
    assert field(emitted[0], "outcome") == "served"
    assert field(emitted[0], "model_version") == "demand_baseline_v1"
    assert str(hotel.public_id) not in emitted[0].getMessage()


def test_counting_a_model_versions_predictions_is_one_query(
    api: TestClient, session: Session, hotel: Hotel
) -> None:
    """The observability read, on the index built for it -- not a scan per hotel."""
    api.get(url(hotel), params=params())

    served = session.execute(
        sa.text(
            "SELECT count(*) FROM demand_predictions"
            " WHERE model_version = :version AND generated_at >= :since"
        ),
        {"version": "demand_baseline_v1", "since": dt.datetime(2000, 1, 1, tzinfo=dt.UTC)},
    ).scalar_one()

    assert served == 1


# --- the migration, as applied --------------------------------------------------------------------


def test_the_table_exists_with_its_identity_constraint(session: Session) -> None:
    """The suite runs `downgrade base` then `upgrade head`, so this is the migration's own work.

    Two unique constraints since Stage 6.11 added ``public_id``, and the set is asserted whole
    rather than as "the identity one is in there somewhere" -- a third arriving unannounced
    should fail here. The Stage 6.8 claim is unchanged: the identity constraint exists, and it
    is still the one this file is responsible for.
    """
    constraints = set(
        session.execute(
            sa.text(
                "SELECT conname FROM pg_constraint"
                " WHERE conrelid = 'demand_predictions'::regclass AND contype = 'u'"
            )
        ).scalars()
    )

    assert constraints == {
        "uq_demand_predictions_identity",
        "uq_demand_predictions_public_id",
    }


def test_the_head_is_the_stage_68_revision(session: Session) -> None:
    revision = session.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()

    assert revision == "0011_demand_prediction_public_id"
