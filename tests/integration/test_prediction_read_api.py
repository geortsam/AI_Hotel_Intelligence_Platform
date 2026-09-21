"""Stage 6.11 against real PostgreSQL, through the real HTTP surface.

The contract's arithmetic -- field sets, bounds, the ordering rule as a constant -- is in
``tests/backend/test_prediction_read.py`` and needs no database. What needs one is everything
here, and none of it could be checked honestly against SQLite or a mock:

* **the migration**, including the backfill of rows that existed before it and its downgrade;
* **every stored row returned**, including two predictions for one target date that Stage 6.9's
  reader collapses to one -- proven side by side in the same test;
* **pagination that neither skips nor repeats**, under the total order PostgreSQL applies;
* **tenant isolation**, with two hotels holding predictions over the same dates in one table;
* **that a read writes nothing**, counted and digested in the database rather than inferred.

All data here is test fixture data, created in the disposable database this suite is given and
truncated with it. The Stage 5.17 guard refuses an unsafe target before a statement runs.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.ml.serving import APPROVED_MODEL
from app.models.hotel import Hotel
from app.repositories.ml_prediction import MlPredictionRepository
from tests.integration.conftest import (
    authenticated_client,
    create_test_app,
    grant_membership,
    make_hotel,
    requires_postgres,
)

pytestmark = requires_postgres

SUITE_EMAIL = "prediction-read@example.test"
OTHER_EMAIL = "prediction-read-other@example.test"

APPROVED_DIGEST = APPROVED_MODEL.canonical_model_digest

WINDOW_FROM = dt.date(2026, 4, 1)
WINDOW_TO = dt.date(2026, 4, 30)

FEATURES: dict[str, float] = {
    "day_of_week": 1.0,
    "day_of_month": 2.0,
    "month": 4.0,
    "week_of_year": 14.0,
    "day_of_year": 92.0,
    "is_weekend": 0.0,
    "demand_lag_7": 100.0,
    "demand_lag_14": 100.0,
    "demand_lag_28": 100.0,
}

EXPECTED_FIELDS = {
    "public_id",
    "target_date",
    "forecast_horizon_days",
    "prediction_cutoff",
    "predicted_room_nights",
    "model_name",
    "model_version",
    "feature_version",
    "dataset_version",
    "generated_at",
}


# --- seeding --------------------------------------------------------------------------------


def record_prediction(
    session: Session,
    hotel: Hotel,
    *,
    target_date: dt.date,
    predicted: float,
    feature_digest: str,
    generated_at: dt.datetime | None = None,
    model_version: str = "demand_baseline_v1",
    digest: str = APPROVED_DIGEST,
) -> None:
    """Insert one prediction with an explicit ``generated_at``, letting ``public_id`` default."""
    session.execute(
        sa.text(
            """
            INSERT INTO demand_predictions (
                hotel_id, target_date, forecast_horizon_days, prediction_cutoff,
                predicted_room_nights, model_name, model_version, feature_version,
                dataset_version, canonical_model_digest, feature_values, feature_digest,
                request_id, generated_at
            ) VALUES (
                :hotel_id, :target_date, 7, :cutoff,
                :predicted, :model_name, :model_version, :feature_version,
                :dataset_version, :digest, CAST(:features AS jsonb), :feature_digest,
                NULL, :generated_at
            )
            """
        ),
        {
            "hotel_id": hotel.id,
            "target_date": target_date,
            "cutoff": dt.datetime.combine(
                target_date - dt.timedelta(days=6), dt.time.min, tzinfo=dt.UTC
            ),
            "predicted": predicted,
            "model_name": APPROVED_MODEL.model_name,
            "model_version": model_version,
            "feature_version": APPROVED_MODEL.feature_version,
            "dataset_version": APPROVED_MODEL.dataset_version,
            "digest": digest,
            "features": json.dumps(FEATURES),
            "feature_digest": feature_digest,
            "generated_at": generated_at or dt.datetime(2026, 3, 1, 12, tzinfo=dt.UTC),
        },
    )
    session.commit()


def seed_hotel(session: Session, *, slug: str) -> Hotel:
    hotel = make_hotel(session, slug=slug)
    session.commit()
    return hotel


def url(hotel: Hotel) -> str:
    return f"/api/v1/hotels/{hotel.public_id}/ml/demand-predictions"


def params(**overrides: object) -> dict[str, object]:
    return {"date_from": str(WINDOW_FROM), "date_to": str(WINDOW_TO), **overrides}


@pytest.fixture
def hotel(session: Session, engine: Engine) -> Hotel:
    return seed_hotel(session, slug="pr-hotel")


@pytest.fixture
def api(engine: Engine, hotel: Hotel) -> TestClient:
    client = authenticated_client(engine, email=SUITE_EMAIL)
    grant_membership(engine, SUITE_EMAIL, str(hotel.public_id), "owner")
    return client


def snapshot(session: Session) -> tuple[int, str | None]:
    """Everything a read could conceivably disturb, in one comparable value."""
    count = int(session.execute(sa.text("SELECT count(*) FROM demand_predictions")).scalar_one())
    digest = session.execute(
        sa.text(
            "SELECT md5(string_agg(public_id::text || predicted_room_nights::text, '|' "
            "ORDER BY id)) FROM demand_predictions"
        )
    ).scalar_one()
    return count, digest


# ======================================================================================
# The migration
# ======================================================================================


def test_every_row_carries_a_distinct_valid_public_id(session: Session, hotel: Hotel) -> None:
    """The backfill and the default, observed on rows this suite inserted.

    The pre-migration backfill itself is exercised by the upgrade/downgrade test below, which
    seeds at 0010 before applying 0011.
    """
    for day in (1, 2, 3):
        record_prediction(
            session,
            hotel,
            target_date=dt.date(2026, 4, day),
            predicted=100.0 + day,
            feature_digest=f"d{day}",
        )

    ids = list(session.execute(sa.text("SELECT public_id FROM demand_predictions")).scalars())

    assert len(ids) == 3
    assert all(isinstance(value, uuid.UUID) for value in ids)
    assert len(set(ids)) == 3


def test_a_duplicate_public_id_is_refused_by_the_constraint(session: Session, hotel: Hotel) -> None:
    record_prediction(
        session, hotel, target_date=dt.date(2026, 4, 1), predicted=100.0, feature_digest="first"
    )
    existing = session.execute(sa.text("SELECT public_id FROM demand_predictions")).scalar_one()

    with pytest.raises(sa.exc.IntegrityError) as caught:
        session.execute(
            sa.text(
                """
                INSERT INTO demand_predictions (
                    hotel_id, target_date, forecast_horizon_days, prediction_cutoff,
                    predicted_room_nights, model_name, model_version, feature_version,
                    dataset_version, canonical_model_digest, feature_values, feature_digest,
                    request_id, public_id
                ) VALUES (:h, :d, 7, :c, 1.0, 'm', 'v', 'v', 'v', :dig, '{}'::jsonb, 'dupe',
                          NULL, :p)
                """
            ),
            {
                "h": hotel.id,
                "d": dt.date(2026, 4, 2),
                "c": dt.datetime(2026, 3, 27, tzinfo=dt.UTC),
                "dig": APPROVED_DIGEST,
                "p": existing,
            },
        )
    session.rollback()

    assert "uq_demand_predictions_public_id" in str(caught.value)


def test_the_migration_backfills_rows_that_existed_before_it(engine: Engine) -> None:
    """Downgrade to 0010, seed, upgrade, and require every pre-existing row to gain a UUID.

    This is the one claim that cannot be made by inspecting the migration: the rows must exist
    *before* the column does. The suite's own schema is restored at the end.
    """
    from alembic import command
    from alembic.config import Config

    config = Config("alembic.ini")
    command.downgrade(config, "0010_demand_predictions")

    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO hotels (name, slug, address_line1, city, country_code,
                                        currency, timezone)
                    VALUES ('Backfill','backfill-probe','1 Road','Town','GR','EUR','UTC')
                    """
                )
            )
            hotel_id = connection.execute(
                sa.text("SELECT id FROM hotels WHERE slug='backfill-probe'")
            ).scalar_one()
            for day in (1, 2, 3):
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO demand_predictions (
                            hotel_id, target_date, forecast_horizon_days, prediction_cutoff,
                            predicted_room_nights, model_name, model_version, feature_version,
                            dataset_version, canonical_model_digest, feature_values,
                            feature_digest, request_id
                        ) VALUES (:h, :d, 7, :c, 100.0, 'm','v1','v1','v1', :dig,
                                  '{}'::jsonb, :fd, NULL)
                        """
                    ),
                    {
                        "h": hotel_id,
                        "d": dt.date(2026, 4, day),
                        "c": dt.datetime(2026, 3, 26, tzinfo=dt.UTC),
                        "dig": APPROVED_DIGEST,
                        "fd": f"pre-{day}",
                    },
                )

        with engine.connect() as connection:
            assert not connection.execute(
                sa.text(
                    "SELECT 1 FROM information_schema.columns WHERE "
                    "table_name='demand_predictions' AND column_name='public_id'"
                )
            ).first(), "public_id must not exist at 0010"

        command.upgrade(config, "head")

        with engine.connect() as connection:
            assert (
                connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
                == "0011_demand_prediction_public_id"
            )
            backfilled = list(
                connection.execute(
                    sa.text(
                        "SELECT public_id FROM demand_predictions WHERE feature_digest LIKE 'pre-%'"
                    )
                ).scalars()
            )
            assert len(backfilled) == 3
            assert all(isinstance(value, uuid.UUID) for value in backfilled)
            assert len(set(backfilled)) == 3

            assert (
                connection.execute(
                    sa.text(
                        "SELECT is_nullable FROM information_schema.columns WHERE "
                        "table_name='demand_predictions' AND column_name='public_id'"
                    )
                ).scalar_one()
                == "NO"
            )
            assert connection.execute(
                sa.text(
                    "SELECT 1 FROM pg_constraint WHERE conname='uq_demand_predictions_public_id'"
                )
            ).first()
            assert (
                connection.execute(
                    sa.text(
                        "SELECT count(*) FROM information_schema.tables WHERE "
                        "table_schema='public' AND table_type='BASE TABLE'"
                    )
                ).scalar_one()
                == 23
            ), "a column, not a table"
    finally:
        # Leave the schema as this suite found it, whatever happened above.
        command.upgrade(config, "head")
        with engine.begin() as connection:
            connection.execute(sa.text("DELETE FROM demand_predictions"))
            connection.execute(sa.text("DELETE FROM hotels WHERE slug='backfill-probe'"))


def test_the_downgrade_removes_only_what_it_added(engine: Engine) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config("alembic.ini")
    command.downgrade(config, "0010_demand_predictions")
    try:
        with engine.connect() as connection:
            assert not connection.execute(
                sa.text(
                    "SELECT 1 FROM information_schema.columns WHERE "
                    "table_name='demand_predictions' AND column_name='public_id'"
                )
            ).first()
            assert not connection.execute(
                sa.text(
                    "SELECT 1 FROM pg_constraint WHERE conname='uq_demand_predictions_public_id'"
                )
            ).first()
            assert connection.execute(
                sa.text(
                    "SELECT 1 FROM information_schema.tables WHERE table_name='demand_predictions'"
                )
            ).first(), "the table survives; only the column goes"
    finally:
        command.upgrade(config, "head")


# ======================================================================================
# Read correctness
# ======================================================================================


def test_every_stored_row_is_returned(api: TestClient, session: Session, hotel: Hotel) -> None:
    for day in (1, 5, 9):
        record_prediction(
            session,
            hotel,
            target_date=dt.date(2026, 4, day),
            predicted=100.0 + day,
            feature_digest=f"d{day}",
        )

    body = api.get(url(hotel), params=params()).json()

    assert body["total"] == 3
    assert [item["target_date"] for item in body["items"]] == [
        "2026-04-01",
        "2026-04-05",
        "2026-04-09",
    ]


def test_two_predictions_for_one_target_date_are_both_returned(
    api: TestClient, session: Session, hotel: Hotel
) -> None:
    """The heart of Stage 6.11, proven beside the Stage 6.9 reader that collapses them.

    Both rows are legitimate history: Stage 6.8 made a repeat a distinct row because a booking
    recorded late changes the inputs. Accuracy scores one of them; disclosure shows both.
    """
    target = dt.date(2026, 4, 5)
    record_prediction(
        session,
        hotel,
        target_date=target,
        predicted=100.0,
        feature_digest="earliest",
        generated_at=dt.datetime(2026, 3, 1, 12, tzinfo=dt.UTC),
    )
    record_prediction(
        session,
        hotel,
        target_date=target,
        predicted=900.0,
        feature_digest="later",
        generated_at=dt.datetime(2026, 3, 9, 12, tzinfo=dt.UTC),
    )

    collapsed = MlPredictionRepository(session).scorable_predictions(hotel.id, target, target)
    body = api.get(url(hotel), params=params()).json()

    assert len(collapsed) == 1, "Stage 6.9 still collapses"
    assert collapsed[0].feature_digest == "earliest"

    assert body["total"] == 2, "Stage 6.11 discloses both"
    assert [item["predicted_room_nights"] for item in body["items"]] == [100.0, 900.0]


def test_two_model_versions_are_both_returned(
    api: TestClient, session: Session, hotel: Hotel
) -> None:
    target = dt.date(2026, 4, 5)
    for version, predicted in (("demand_baseline_v1", 100.0), ("demand_baseline_v2", 200.0)):
        record_prediction(
            session,
            hotel,
            target_date=target,
            predicted=predicted,
            feature_digest=version,
            model_version=version,
        )

    body = api.get(url(hotel), params=params()).json()

    assert body["total"] == 2
    assert {item["model_version"] for item in body["items"]} == {
        "demand_baseline_v1",
        "demand_baseline_v2",
    }


@pytest.mark.parametrize("day", [1, 30])
def test_the_window_boundaries_are_inclusive(
    api: TestClient, session: Session, hotel: Hotel, day: int
) -> None:
    record_prediction(
        session,
        hotel,
        target_date=dt.date(2026, 4, day),
        predicted=100.0,
        feature_digest="boundary",
    )

    assert api.get(url(hotel), params=params()).json()["total"] == 1


@pytest.mark.parametrize("target", [dt.date(2026, 3, 31), dt.date(2026, 5, 1)])
def test_a_row_outside_the_window_is_excluded(
    api: TestClient, session: Session, hotel: Hotel, target: dt.date
) -> None:
    record_prediction(session, hotel, target_date=target, predicted=100.0, feature_digest="outside")

    assert api.get(url(hotel), params=params()).json()["total"] == 0


def test_the_response_matches_the_stored_row_field_for_field(
    api: TestClient, session: Session, hotel: Hotel
) -> None:
    record_prediction(
        session,
        hotel,
        target_date=dt.date(2026, 4, 5),
        predicted=137.5,
        feature_digest="exact",
        generated_at=dt.datetime(2026, 3, 9, 12, tzinfo=dt.UTC),
    )
    stored = session.execute(sa.text("SELECT * FROM demand_predictions")).mappings().one()

    item = api.get(url(hotel), params=params()).json()["items"][0]

    assert set(item) == EXPECTED_FIELDS
    assert item["public_id"] == str(stored["public_id"])
    assert item["target_date"] == stored["target_date"].isoformat()
    assert item["forecast_horizon_days"] == stored["forecast_horizon_days"]
    assert item["predicted_room_nights"] == pytest.approx(float(stored["predicted_room_nights"]))
    assert item["model_name"] == stored["model_name"]
    assert item["model_version"] == stored["model_version"]
    assert item["feature_version"] == stored["feature_version"]
    assert item["dataset_version"] == stored["dataset_version"]


def test_no_internal_identifier_reaches_the_response(
    api: TestClient, session: Session, hotel: Hotel
) -> None:
    record_prediction(
        session, hotel, target_date=dt.date(2026, 4, 5), predicted=100.0, feature_digest="secret"
    )
    internal = session.execute(sa.text("SELECT id FROM demand_predictions")).scalar_one()

    raw = api.get(url(hotel), params=params()).text

    assert "hotel_id" not in raw
    assert "feature_values" not in raw
    assert "feature_digest" not in raw
    assert "canonical_model_digest" not in raw
    assert "request_id" not in raw
    assert f'"id":{internal}' not in raw.replace(" ", "")


# ======================================================================================
# Pagination
# ======================================================================================


def test_concatenated_pages_reproduce_the_full_ordered_set_exactly_once(
    api: TestClient, session: Session, hotel: Hotel
) -> None:
    """The property a total order exists to give: no duplicate, no omission."""
    for day in range(1, 8):
        record_prediction(
            session,
            hotel,
            target_date=dt.date(2026, 4, day),
            predicted=100.0 + day,
            feature_digest=f"d{day}",
        )

    whole = api.get(url(hotel), params=params(page_size=100)).json()
    walked: list[str] = []
    for page in (1, 2, 3):
        body = api.get(url(hotel), params=params(page=page, page_size=3)).json()
        walked.extend(item["public_id"] for item in body["items"])
        assert body["total"] == 7
        assert body["pages"] == 3

    assert walked == [item["public_id"] for item in whole["items"]]
    assert len(walked) == len(set(walked)) == 7


def test_a_page_beyond_the_last_is_empty_with_the_true_total(
    api: TestClient, session: Session, hotel: Hotel
) -> None:
    record_prediction(
        session, hotel, target_date=dt.date(2026, 4, 5), predicted=100.0, feature_digest="only"
    )

    body = api.get(url(hotel), params=params(page=9)).json()

    assert body["items"] == []
    assert body["total"] == 1
    assert body["pages"] == 1


def test_an_empty_window_is_an_empty_page(api: TestClient, hotel: Hotel) -> None:
    response = api.get(url(hotel), params=params())

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "page": 1, "page_size": 20, "pages": 0}


@pytest.mark.parametrize(
    "overrides",
    [
        {"page": 0},
        {"page_size": 0},
        {"page_size": 101},
        {"date_from": "2026-04-30", "date_to": "2026-04-01"},
        {"date_from": "not-a-date"},
    ],
)
def test_a_malformed_request_is_refused(
    api: TestClient, hotel: Hotel, overrides: dict[str, object]
) -> None:
    assert api.get(url(hotel), params=params(**overrides)).status_code == 422


def test_the_window_is_required(api: TestClient, hotel: Hotel) -> None:
    assert api.get(url(hotel)).status_code == 422
    assert api.get(url(hotel), params={"date_from": str(WINDOW_FROM)}).status_code == 422


# ======================================================================================
# Security
# ======================================================================================


def test_an_unauthenticated_request_is_refused(engine: Engine, hotel: Hotel) -> None:
    anonymous = TestClient(create_test_app(engine))

    assert anonymous.get(url(hotel), params=params()).status_code == 401


def test_a_non_member_receives_the_hotels_own_404(
    engine: Engine, session: Session, hotel: Hotel
) -> None:
    record_prediction(
        session, hotel, target_date=dt.date(2026, 4, 5), predicted=100.0, feature_digest="hidden"
    )
    outsider = authenticated_client(engine, email=OTHER_EMAIL)

    response = outsider.get(url(hotel), params=params())

    assert response.status_code == 404
    assert "hidden" not in response.text


def test_an_unknown_hotel_is_refused_identically(engine: Engine, hotel: Hotel) -> None:
    """Indistinguishable from a hotel that exists and is not yours, so neither reveals the other."""
    outsider = authenticated_client(engine, email=OTHER_EMAIL)

    unknown = outsider.get(f"/api/v1/hotels/{uuid.uuid4()}/ml/demand-predictions", params=params())
    non_member = outsider.get(url(hotel), params=params())

    assert unknown.status_code == non_member.status_code == 404
    assert unknown.json() == non_member.json()


def test_one_hotels_predictions_are_not_reachable_through_another(
    api: TestClient, engine: Engine, session: Session, hotel: Hotel
) -> None:
    other = seed_hotel(session, slug="pr-other")
    grant_membership(engine, SUITE_EMAIL, str(other.public_id), "owner")

    record_prediction(
        session, hotel, target_date=dt.date(2026, 4, 5), predicted=100.0, feature_digest="mine"
    )
    record_prediction(
        session, other, target_date=dt.date(2026, 4, 5), predicted=900.0, feature_digest="theirs"
    )

    mine = api.get(url(hotel), params=params()).json()
    theirs = api.get(url(other), params=params()).json()

    assert mine["total"] == theirs["total"] == 1
    assert mine["items"][0]["predicted_room_nights"] == 100.0
    assert theirs["items"][0]["predicted_room_nights"] == 900.0
    assert mine["items"][0]["public_id"] != theirs["items"][0]["public_id"]


# ======================================================================================
# Read-only and deterministic
# ======================================================================================


def test_a_read_leaves_the_database_unchanged(
    api: TestClient, session: Session, hotel: Hotel
) -> None:
    for day in (1, 5, 9):
        record_prediction(
            session,
            hotel,
            target_date=dt.date(2026, 4, day),
            predicted=100.0 + day,
            feature_digest=f"d{day}",
        )
    before = snapshot(session)

    api.get(url(hotel), params=params())
    session.rollback()

    assert snapshot(session) == before


def test_two_identical_requests_return_byte_identical_bodies(
    api: TestClient, session: Session, hotel: Hotel
) -> None:
    for day in (1, 5, 9):
        record_prediction(
            session,
            hotel,
            target_date=dt.date(2026, 4, day),
            predicted=100.0 + day,
            feature_digest=f"d{day}",
        )

    assert api.get(url(hotel), params=params()).text == api.get(url(hotel), params=params()).text


def test_the_read_is_two_statements_regardless_of_window_size(
    session: Session, hotel: Hotel
) -> None:
    """One count and one page, whether the window holds one row or twenty. No N+1."""
    for day in range(1, 21):
        record_prediction(
            session,
            hotel,
            target_date=dt.date(2026, 4, day),
            predicted=100.0 + day,
            feature_digest=f"d{day}",
        )

    statements: list[str] = []

    def listener(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        statements.append(statement)

    sa.event.listen(session.get_bind(), "before_cursor_execute", listener)
    try:
        MlPredictionRepository(session).stored_predictions_page(
            hotel.id, WINDOW_FROM, WINDOW_TO, page=1, page_size=20
        )
    finally:
        sa.event.remove(session.get_bind(), "before_cursor_execute", listener)

    reads = [s for s in statements if "demand_predictions" in s]
    assert len(reads) == 2, reads


def test_the_alembic_head_is_the_stage_611_revision(session: Session) -> None:
    revision = session.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()

    assert revision == "0011_demand_prediction_public_id"


# ======================================================================================
# The event
# ======================================================================================


class RecordingHandler(logging.Handler):
    """Collects records instead of formatting them."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def events() -> Iterator[RecordingHandler]:
    """Attached to the module's own logger: caplog stops capturing once any test starts an app."""
    handler = RecordingHandler()
    logger = logging.getLogger("app.services.ml_prediction_read")
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


STDLIB_RECORD_FIELDS = frozenset(
    logging.LogRecord("n", logging.INFO, "p", 1, "m", None, None).__dict__
) | {"message", "asctime", "request_id"}


def test_one_request_emits_one_event_naming_nothing_sensitive(
    api: TestClient, session: Session, hotel: Hotel, events: RecordingHandler
) -> None:
    record_prediction(
        session, hotel, target_date=dt.date(2026, 4, 5), predicted=8317.25, feature_digest="e" * 64
    )

    api.get(url(hotel), params=params())

    emitted = [record for record in events.records if hasattr(record, "outcome")]
    assert len(emitted) == 1

    attached = {
        name: value
        for name, value in emitted[0].__dict__.items()
        if name not in STDLIB_RECORD_FIELDS
    }
    assert set(attached) == {"outcome", "model_version", "predictions_returned", "duration_ms"}
    assert attached["outcome"] == "returned"
    assert attached["predictions_returned"] == 1

    rendered = emitted[0].getMessage() + repr(
        {k: v for k, v in attached.items() if k != "duration_ms"}
    )
    for forbidden in (
        "8317.25",
        "e" * 64,
        APPROVED_DIGEST,
        str(hotel.public_id),
        "2026-04",
        "SELECT",
    ):
        assert forbidden not in rendered, forbidden
