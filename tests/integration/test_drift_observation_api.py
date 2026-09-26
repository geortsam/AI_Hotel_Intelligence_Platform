"""Stage 6.10 against real PostgreSQL: summaries over rows the platform actually stored.

The protocol's arithmetic -- quantiles, empty and single-observation semantics, the comparison
rules -- is in ``tests/backend/test_drift_observation.py`` and needs no database. What needs one
is everything here:

* **summaries over real stored rows**, including the JSONB feature object as PostgreSQL returns
  it rather than as a Python literal;
* **the inherited Stage 6.9 selection rule**, executed by ``DISTINCT ON`` -- two predictions for
  one target date must summarise as one;
* **tenant isolation**, with two hotels holding predictions over the same dates in one table;
* **a non-member refused before anything is read**, through the real resolver over a real user;
* **that an observation writes nothing**, counted and digested in the database rather than
  inferred from the source.

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
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.ml.drift_protocol import PROTOCOL
from app.ml.serving import APPROVED_MODEL
from app.models.hotel import Hotel
from app.models.user import User
from app.repositories.hotel import HotelRepository
from app.repositories.membership import MembershipRepository
from app.repositories.ml_prediction import MlPredictionRepository
from app.repositories.room_type import RoomTypeRepository
from app.schemas.ml_drift import DistributionObservation
from app.services.authorization import HotelAccessPolicy
from app.services.ml_drift import DemandDistributionService
from app.services.scope import HotelScopeResolver
from tests.integration.conftest import (
    authenticated_client,
    grant_membership,
    make_hotel,
    requires_postgres,
)

pytestmark = requires_postgres

SUITE_EMAIL = "drift-observation@example.test"
OTHER_EMAIL = "drift-observation-other@example.test"

APPROVED_DIGEST = APPROVED_MODEL.canonical_model_digest

WINDOW = (dt.date(2026, 4, 1), dt.date(2026, 4, 30))
BASELINE = (dt.date(2026, 3, 1), dt.date(2026, 3, 31))


def features(level: float) -> dict[str, float]:
    return {
        "day_of_week": 1.0,
        "day_of_month": 2.0,
        "month": 4.0,
        "week_of_year": 14.0,
        "day_of_year": 92.0,
        "is_weekend": 0.0,
        "demand_lag_7": level,
        "demand_lag_14": level,
        "demand_lag_28": level,
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
    level: float = 100.0,
    model_version: str = "demand_baseline_v1",
    digest: str = APPROVED_DIGEST,
) -> None:
    """Insert one prediction with an explicit ``generated_at``.

    Written as SQL rather than through ``MlPredictionRepository.record``: that method lets the
    database default ``generated_at``, and the selection test below is about rows that differ by
    exactly that column.
    """
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
            "features": json.dumps(features(level)),
            "feature_digest": feature_digest,
            "generated_at": generated_at or dt.datetime(2026, 2, 1, 12, tzinfo=dt.UTC),
        },
    )
    session.commit()


def seed_hotel(session: Session, *, slug: str) -> Hotel:
    hotel = make_hotel(session, slug=slug)
    session.commit()
    return hotel


# --- the service, wired to the real authorization chain ----------------------------------------


def build_service(session: Session, *, email: str = SUITE_EMAIL) -> DemandDistributionService:
    """The real resolver over a real user, so a non-member is refused for the real reason."""
    user = session.execute(sa.select(User).where(User.email == email)).scalar_one()
    policy = HotelAccessPolicy(user, MembershipRepository(session))
    scope = HotelScopeResolver(HotelRepository(session), RoomTypeRepository(session), policy)
    return DemandDistributionService(MlPredictionRepository(session), scope)


def observe(
    service: DemandDistributionService,
    hotel: Hotel,
    *,
    baseline: tuple[dt.date, dt.date] | None = None,
) -> DistributionObservation:
    return service.observe(
        hotel.public_id, window_from=WINDOW[0], window_to=WINDOW[1], baseline=baseline
    )


def summary_of(
    result: DistributionObservation, name: str, *, segment: str = "within_calibration"
) -> Any:
    entry = result.observed.by_model_version[0]
    return next(field for field in getattr(entry, segment).fields if field.field == name)


@pytest.fixture
def hotel(session: Session, engine: Engine) -> Hotel:
    hotel = seed_hotel(session, slug="do-hotel")
    authenticated_client(engine, email=SUITE_EMAIL)
    grant_membership(engine, SUITE_EMAIL, str(hotel.public_id), "owner")
    return hotel


def snapshot(session: Session) -> tuple[int, str | None]:
    """Everything an observation could conceivably disturb, in one comparable value."""
    count = int(session.execute(sa.text("SELECT count(*) FROM demand_predictions")).scalar_one())
    digest = session.execute(
        sa.text(
            "SELECT md5(string_agg(feature_digest || predicted_room_nights::text, '|' "
            "ORDER BY id)) FROM demand_predictions"
        )
    ).scalar_one()
    return count, digest


# ======================================================================================
# Summaries over real rows
# ======================================================================================


def test_real_stored_rows_are_summarised(session: Session, hotel: Hotel) -> None:
    """Predictions 100, 140 and 180 over one window: mean 140, median 140, min 100, max 180."""
    for day, predicted in ((5, 100.0), (7, 140.0), (9, 180.0)):
        record_prediction(
            session,
            hotel,
            target_date=dt.date(2026, 4, day),
            predicted=predicted,
            feature_digest=f"d{day}",
        )

    result = observe(build_service(session), hotel)
    predicted = summary_of(result, "predicted_room_nights")

    assert result.observed.candidates == 3
    assert predicted.count == 3
    assert predicted.minimum == pytest.approx(100.0)
    assert predicted.maximum == pytest.approx(180.0)
    assert predicted.mean == pytest.approx(140.0)
    assert predicted.median == pytest.approx(140.0)


def test_the_jsonb_feature_object_is_summarised_as_postgresql_returns_it(
    session: Session, hotel: Hotel
) -> None:
    """The nine features come back through JSONB, not from a Python literal."""
    record_prediction(
        session,
        hotel,
        target_date=dt.date(2026, 4, 5),
        predicted=120.0,
        feature_digest="jsonb",
        level=77.0,
    )

    result = observe(build_service(session), hotel)
    fields = result.observed.by_model_version[0].within_calibration.fields

    assert tuple(field.field for field in fields) == PROTOCOL.observed_fields
    assert summary_of(result, "demand_lag_7").mean == pytest.approx(77.0)
    assert summary_of(result, "demand_lag_28").maximum == pytest.approx(77.0)
    assert summary_of(result, "day_of_week").minimum == pytest.approx(1.0)


def test_an_empty_window_summarises_to_nothing(session: Session, hotel: Hotel) -> None:
    result = observe(build_service(session), hotel)

    assert result.observed.candidates == 0
    assert result.observed.by_model_version == ()
    assert result.comparison is None


def test_a_single_stored_row_summarises_without_error(session: Session, hotel: Hotel) -> None:
    record_prediction(
        session, hotel, target_date=dt.date(2026, 4, 5), predicted=123.5, feature_digest="one"
    )

    predicted = summary_of(observe(build_service(session), hotel), "predicted_room_nights")

    assert predicted.count == 1
    assert predicted.mean == pytest.approx(123.5)
    assert predicted.median == pytest.approx(123.5)
    assert all(value == pytest.approx(123.5) for _, value in predicted.quantiles)


# ======================================================================================
# The inherited selection rule
# ======================================================================================


def test_two_rows_for_one_target_date_summarise_as_one(session: Session, hotel: Hotel) -> None:
    """Stage 6.9's rule, executed by DISTINCT ON: the earliest generated_at wins."""
    target = dt.date(2026, 4, 5)
    record_prediction(
        session,
        hotel,
        target_date=target,
        predicted=100.0,
        feature_digest="earliest",
        generated_at=dt.datetime(2026, 2, 1, 12, tzinfo=dt.UTC),
    )
    record_prediction(
        session,
        hotel,
        target_date=target,
        predicted=900.0,
        feature_digest="later",
        generated_at=dt.datetime(2026, 2, 9, 12, tzinfo=dt.UTC),
    )

    result = observe(build_service(session), hotel)
    segment = result.observed.by_model_version[0].within_calibration

    assert result.observed.candidates == 1
    assert segment.observations == 1
    assert segment.feature_digests == ("earliest",)
    assert summary_of(result, "predicted_room_nights").mean == pytest.approx(100.0)


def test_one_query_per_window(session: Session, hotel: Hotel) -> None:
    """No N+1: ten target dates cost the same one statement as one."""
    for day in range(1, 11):
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
        observe(build_service(session), hotel, baseline=BASELINE)
    finally:
        sa.event.remove(session.get_bind(), "before_cursor_execute", listener)

    reads = [s for s in statements if "FROM demand_predictions" in s]
    assert len(reads) == 2, reads


# ======================================================================================
# Windows and comparison
# ======================================================================================


def test_two_windows_produce_summaries_and_movement(session: Session, hotel: Hotel) -> None:
    record_prediction(
        session, hotel, target_date=dt.date(2026, 4, 5), predicted=150.0, feature_digest="now"
    )
    record_prediction(
        session, hotel, target_date=dt.date(2026, 3, 5), predicted=100.0, feature_digest="then"
    )

    result = observe(build_service(session), hotel, baseline=BASELINE)

    assert result.comparison is not None
    assert result.comparison.baseline.window_from == BASELINE[0]
    assert result.comparison.baseline.candidates == 1
    moved = result.comparison.by_model_version[0].within_calibration
    predicted = next(field for field in moved.fields if field.field == "predicted_room_nights")
    assert predicted.mean == pytest.approx(50.0)


def test_a_missing_baseline_leaves_the_comparison_absent(session: Session, hotel: Hotel) -> None:
    record_prediction(
        session, hotel, target_date=dt.date(2026, 4, 5), predicted=150.0, feature_digest="solo"
    )

    assert observe(build_service(session), hotel).comparison is None


def test_the_same_window_as_its_own_baseline_moves_by_zero(session: Session, hotel: Hotel) -> None:
    record_prediction(
        session, hotel, target_date=dt.date(2026, 4, 5), predicted=150.0, feature_digest="self"
    )

    service = build_service(session)
    result = service.observe(
        hotel.public_id, window_from=WINDOW[0], window_to=WINDOW[1], baseline=WINDOW
    )

    assert result.comparison is not None
    moved = result.comparison.by_model_version[0].within_calibration
    predicted = next(field for field in moved.fields if field.field == "predicted_room_nights")
    assert predicted.mean == pytest.approx(0.0)
    assert predicted.count == 0


# ======================================================================================
# Segmentation, model version and digest
# ======================================================================================


def test_the_two_segments_are_summarised_separately(session: Session, hotel: Hotel) -> None:
    record_prediction(
        session,
        hotel,
        target_date=dt.date(2026, 4, 5),
        predicted=165.0,
        feature_digest="small",
        level=10.0,
    )
    record_prediction(
        session,
        hotel,
        target_date=dt.date(2026, 4, 6),
        predicted=150.0,
        feature_digest="large",
        level=200.0,
    )

    entry = observe(build_service(session), hotel).observed.by_model_version[0]

    assert entry.below_calibration.observations == 1
    assert entry.below_calibration.feature_digests == ("small",)
    assert entry.within_calibration.observations == 1
    assert entry.within_calibration.feature_digests == ("large",)
    below = next(field for field in entry.below_calibration.fields if field.field == "demand_lag_7")
    assert below.maximum == pytest.approx(10.0)


def test_two_model_versions_are_summarised_separately(session: Session, hotel: Hotel) -> None:
    record_prediction(
        session,
        hotel,
        target_date=dt.date(2026, 4, 5),
        predicted=100.0,
        feature_digest="v1",
        model_version="demand_baseline_v1",
    )
    record_prediction(
        session,
        hotel,
        target_date=dt.date(2026, 4, 5),
        predicted=400.0,
        feature_digest="v2",
        model_version="demand_baseline_v2",
    )

    result = observe(build_service(session), hotel)

    assert [entry.model_version for entry in result.observed.by_model_version] == [
        "demand_baseline_v1",
        "demand_baseline_v2",
    ]
    means = [
        next(f for f in entry.within_calibration.fields if f.field == "predicted_room_nights").mean
        for entry in result.observed.by_model_version
    ]
    assert means == pytest.approx([100.0, 400.0])


def test_a_foreign_digest_is_reported_and_summarised_into_nothing(
    session: Session, hotel: Hotel
) -> None:
    record_prediction(
        session, hotel, target_date=dt.date(2026, 4, 5), predicted=100.0, feature_digest="ours"
    )
    record_prediction(
        session,
        hotel,
        target_date=dt.date(2026, 4, 6),
        predicted=100.0,
        feature_digest="theirs",
        digest="0" * 64,
    )

    result = observe(build_service(session), hotel)

    assert result.observed.candidates == 2
    assert result.observed.out_of_scope_model_digest == 1
    assert len(result.observed.by_model_version) == 1
    assert result.observed.by_model_version[0].within_calibration.observations == 1
    assert result.observed.by_model_version[0].canonical_model_digest == APPROVED_DIGEST


# ======================================================================================
# Tenant isolation
# ======================================================================================


def test_one_hotels_predictions_never_enter_anothers_observation(
    session: Session, engine: Engine, hotel: Hotel
) -> None:
    other = seed_hotel(session, slug="do-other")
    grant_membership(engine, SUITE_EMAIL, str(other.public_id), "owner")

    record_prediction(
        session, hotel, target_date=dt.date(2026, 4, 5), predicted=100.0, feature_digest="mine"
    )
    record_prediction(
        session, other, target_date=dt.date(2026, 4, 5), predicted=900.0, feature_digest="theirs"
    )

    service = build_service(session)
    mine = observe(service, hotel)
    theirs = observe(service, other)

    assert mine.observed.by_model_version[0].within_calibration.feature_digests == ("mine",)
    assert theirs.observed.by_model_version[0].within_calibration.feature_digests == ("theirs",)
    assert summary_of(mine, "predicted_room_nights").mean == pytest.approx(100.0)
    assert summary_of(theirs, "predicted_room_nights").mean == pytest.approx(900.0)
    assert mine.hotel_public_id != theirs.hotel_public_id


def test_a_non_member_is_refused_before_anything_is_read(
    session: Session, engine: Engine, hotel: Hotel
) -> None:
    authenticated_client(engine, email=OTHER_EMAIL)
    record_prediction(
        session, hotel, target_date=dt.date(2026, 4, 5), predicted=100.0, feature_digest="hidden"
    )

    with pytest.raises(NotFoundError):
        observe(build_service(session, email=OTHER_EMAIL), hotel)


def test_an_unknown_hotel_is_refused_the_same_way(session: Session, hotel: Hotel) -> None:
    service = build_service(session)

    with pytest.raises(NotFoundError):
        service.observe(uuid.uuid4(), window_from=WINDOW[0], window_to=WINDOW[1])


def test_the_result_carries_no_internal_identifier(session: Session, hotel: Hotel) -> None:
    record_prediction(
        session, hotel, target_date=dt.date(2026, 4, 5), predicted=100.0, feature_digest="public"
    )

    rendered = str(observe(build_service(session), hotel).as_dict())

    assert str(hotel.public_id) in rendered
    assert f'"hotel_id": {hotel.id}' not in rendered
    assert "hotel_id" not in rendered


# ======================================================================================
# No writes, and determinism
# ======================================================================================


def test_an_observation_leaves_the_database_unchanged(session: Session, hotel: Hotel) -> None:
    for day in (5, 6, 7):
        record_prediction(
            session,
            hotel,
            target_date=dt.date(2026, 4, day),
            predicted=100.0 + day,
            feature_digest=f"d{day}",
        )
    before = snapshot(session)

    observe(build_service(session), hotel, baseline=BASELINE)
    session.rollback()

    assert snapshot(session) == before


def test_two_observations_of_the_same_windows_are_bit_identical(
    session: Session, hotel: Hotel
) -> None:
    for day in (5, 6, 7):
        record_prediction(
            session,
            hotel,
            target_date=dt.date(2026, 4, day),
            predicted=100.0 + day,
            feature_digest=f"d{day}",
        )
    record_prediction(
        session, hotel, target_date=dt.date(2026, 3, 5), predicted=80.0, feature_digest="base"
    )

    service = build_service(session)

    assert (
        observe(service, hotel, baseline=BASELINE).as_dict()
        == observe(service, hotel, baseline=BASELINE).as_dict()
    )


def test_no_accuracy_or_distribution_table_was_created(session: Session) -> None:
    """Compute-and-return only: there is nowhere for a result to have been written."""
    tables = list(
        session.execute(
            sa.text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public' "
                "AND (table_name LIKE '%drift%' OR table_name LIKE '%distribution%' "
                "OR table_name LIKE '%observation%')"
            )
        ).scalars()
    )

    assert tables == []


def test_the_alembic_head_is_still_the_stage_68_revision(session: Session) -> None:
    revision = session.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()

    assert revision == "0015_copilot_conversations"


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
    logger = logging.getLogger("app.services.ml_drift")
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


def test_one_observation_emits_one_event_naming_nothing_sensitive(
    session: Session, hotel: Hotel, events: RecordingHandler
) -> None:
    record_prediction(
        session,
        hotel,
        target_date=dt.date(2026, 4, 5),
        predicted=8317.25,
        feature_digest="e" * 64,
        level=6143.0,
    )

    observe(build_service(session), hotel, baseline=BASELINE)

    emitted = [record for record in events.records if hasattr(record, "outcome")]
    assert len(emitted) == 1

    attached = {
        name: value
        for name, value in emitted[0].__dict__.items()
        if name not in STDLIB_RECORD_FIELDS
    }
    assert set(attached) == {
        "outcome",
        "model_version",
        "predictions_summarised",
        "windows_compared",
        "duration_ms",
    }
    assert attached["outcome"] == "observed"
    assert attached["predictions_summarised"] == 1
    assert attached["windows_compared"] == 2

    rendered = emitted[0].getMessage() + repr(
        {k: v for k, v in attached.items() if k != "duration_ms"}
    )
    for forbidden in (
        "8317.25",
        "6143",
        "e" * 64,
        APPROVED_DIGEST,
        str(hotel.public_id),
        "demand_lag_7",
        "median",
        "SELECT",
    ):
        assert forbidden not in rendered, forbidden
