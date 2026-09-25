"""Stage 6.9 against real PostgreSQL: the selection rule, the boundary, and the isolation.

The protocol's arithmetic -- eligibility, segments, metric values -- is in
``tests/backend/test_forecast_accuracy.py`` and needs no database. What needs one is everything
here, and none of it could be checked honestly against SQLite or a mock:

* **the selection rule as the database executes it** -- ``DISTINCT ON`` ordered by
  ``generated_at`` then ``id``, including the tie-break, which no Python-side sort would prove;
* **the settlement boundary over real rows**, at exactly twenty-eight days and one day short;
* **unsettled-allocation detection**, against bookings in real statuses;
* **tenant isolation**, with two hotels holding predictions for the same dates in one table;
* **that an evaluation writes nothing**, counted and digested in the database rather than
  inferred from the source.

The authorization chain is the real one -- ``HotelScopeResolver`` over a real ``User`` and real
memberships -- so "a non-member gets the hotel's own 404" is exercised rather than stubbed.

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
from app.ml.accuracy_protocol import PROTOCOL, SETTLEMENT_LAG_DAYS
from app.ml.serving import APPROVED_MODEL
from app.models.hotel import Hotel
from app.models.room import Room, RoomType
from app.models.user import User
from app.repositories.hotel import HotelRepository
from app.repositories.membership import MembershipRepository
from app.repositories.ml_demand import MlDemandRepository
from app.repositories.ml_prediction import MlPredictionRepository
from app.repositories.room_type import RoomTypeRepository
from app.schemas.ml_accuracy import AccuracyEvaluation
from app.services.authorization import HotelAccessPolicy
from app.services.ml_accuracy import DemandAccuracyService
from app.services.scope import HotelScopeResolver
from tests.integration.conftest import (
    allocate_room,
    authenticated_client,
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

SUITE_EMAIL = "forecast-accuracy@example.test"
OTHER_EMAIL = "forecast-accuracy-other@example.test"

APPROVED_DIGEST = APPROVED_MODEL.canonical_model_digest

#: One as-of date for the whole suite, so every boundary is arithmetic a reader can follow.
AS_OF = dt.date(2026, 6, 1)
#: The newest target date that has cleared the lag at :data:`AS_OF`.
SETTLED = AS_OF - dt.timedelta(days=SETTLEMENT_LAG_DAYS)

FEATURES: dict[str, float] = {
    "day_of_week": 1.0,
    "day_of_month": 2.0,
    "month": 5.0,
    "week_of_year": 19.0,
    "day_of_year": 124.0,
    "is_weekend": 0.0,
    "demand_lag_7": 100.0,
    "demand_lag_14": 100.0,
    "demand_lag_28": 100.0,
}
SMALL_FEATURES = {**FEATURES, "demand_lag_7": 5.0, "demand_lag_14": 5.0, "demand_lag_28": 5.0}


# --- seeding --------------------------------------------------------------------------------


def occupy(
    session: Session,
    hotel: Hotel,
    room: Room,
    night: dt.date,
    *,
    status: str = "checked_out",
) -> None:
    """One complete one-night booking, which is one room night of realised demand.

    ``status`` is the lever the unsettled-allocation tests pull: ``checked_out`` is terminal, so
    the night is final, while ``confirmed`` can still become a no-show.
    """
    guest = make_guest(session, hotel)
    # The status is set on the BOOKING, not patched onto the allocation afterwards: a composite
    # foreign key ties (booking_id, check_in_date, check_out_date, booking_status) to the parent,
    # so the two can never disagree even for an instant. `allocate_room` mirrors the parent.
    booking = make_booking(
        session,
        hotel,
        guest,
        check_in=night,
        check_out=night + dt.timedelta(days=1),
        status=status,
    )
    booking.booked_at = dt.datetime.combine(
        night - dt.timedelta(days=90), dt.time(12), tzinfo=dt.UTC
    )
    session.flush()
    price_nights(session, allocate_room(session, booking, room), ["100.00"])


def seed_hotel(session: Session, *, slug: str) -> tuple[Hotel, Room]:
    hotel = make_hotel(session, slug=slug)
    room_type: RoomType = make_room_type(session, hotel)
    room = make_room(session, hotel, room_type, number="101")
    session.commit()
    return hotel, room


def record_prediction(
    session: Session,
    hotel: Hotel,
    *,
    target_date: dt.date,
    predicted: float,
    generated_at: dt.datetime,
    feature_digest: str,
    model_version: str = "demand_baseline_v1",
    digest: str = APPROVED_DIGEST,
    features: dict[str, float] | None = None,
) -> None:
    """Insert one prediction with an explicit ``generated_at``.

    Written as SQL rather than through ``MlPredictionRepository.record``, deliberately: that
    method lets the database default ``generated_at``, and these tests are about what happens
    when several rows differ by exactly that column.
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
            "features": json.dumps(features or FEATURES),
            "feature_digest": feature_digest,
            "generated_at": generated_at,
        },
    )
    session.commit()


def moment(day: int, hour: int = 12) -> dt.datetime:
    """A fixed instant, so ``generated_at`` ordering is stated rather than incidental."""
    return dt.datetime(2026, 5, day, hour, tzinfo=dt.UTC)


# --- the service, wired to the real authorization chain ----------------------------------------


def build_service(session: Session, *, email: str = SUITE_EMAIL) -> DemandAccuracyService:
    """The real resolver over a real user, so a non-member is refused for the real reason."""
    user = session.execute(sa.select(User).where(User.email == email)).scalar_one()
    policy = HotelAccessPolicy(user, MembershipRepository(session))
    scope = HotelScopeResolver(HotelRepository(session), RoomTypeRepository(session), policy)
    return DemandAccuracyService(
        MlPredictionRepository(session), MlDemandRepository(session), scope
    )


def evaluate(
    service: DemandAccuracyService,
    hotel: Hotel,
    *,
    window_from: dt.date,
    window_to: dt.date,
    as_of_date: dt.date = AS_OF,
) -> AccuracyEvaluation:
    return service.evaluate(
        hotel.public_id,
        as_of_date=as_of_date,
        window_from=window_from,
        window_to=window_to,
    )


@pytest.fixture
def hotel(session: Session, engine: Engine) -> Hotel:
    hotel, _ = seed_hotel(session, slug="fa-hotel")
    authenticated_client(engine, email=SUITE_EMAIL)
    grant_membership(engine, SUITE_EMAIL, str(hotel.public_id), "owner")
    return hotel


@pytest.fixture
def room(session: Session, hotel: Hotel) -> Room:
    return session.execute(sa.select(Room).where(Room.hotel_id == hotel.id)).scalars().first()  # type: ignore[return-value]


def snapshot(session: Session) -> tuple[int, str | None, int]:
    """Everything an evaluation could conceivably disturb, in one comparable value."""
    predictions = int(
        session.execute(sa.text("SELECT count(*) FROM demand_predictions")).scalar_one()
    )
    digest = session.execute(
        sa.text(
            "SELECT md5(string_agg(feature_digest || predicted_room_nights::text, '|' "
            "ORDER BY id)) FROM demand_predictions"
        )
    ).scalar_one()
    nights = int(session.execute(sa.text("SELECT count(*) FROM booking_room_nights")).scalar_one())
    return predictions, digest, nights


# ======================================================================================
# Selection, as the database executes it
# ======================================================================================


def test_the_earliest_prediction_is_the_one_scored(
    session: Session, hotel: Hotel, room: Room
) -> None:
    """Criterion 9. Three rows in one group, three different instants, one scored."""
    occupy(session, hotel, room, SETTLED)
    for day, predicted, digest in (
        (12, 100.0, "first"),
        (14, 200.0, "second"),
        (16, 300.0, "third"),
    ):
        record_prediction(
            session,
            hotel,
            target_date=SETTLED,
            predicted=predicted,
            generated_at=moment(day),
            feature_digest=digest,
        )

    result = evaluate(build_service(session), hotel, window_from=SETTLED, window_to=SETTLED)
    segment = result.by_model_version[0].within_calibration

    assert result.candidates == 1
    assert segment.observations == 1
    assert segment.scored_feature_digests == ("first",)
    assert segment.metrics.mae == pytest.approx(99.0)


def test_a_later_prediction_does_not_change_an_already_scored_date(
    session: Session, hotel: Hotel, room: Room
) -> None:
    """Criterion 10. The decisive property of earliest-not-latest.

    If the newest row were scored, re-requesting a forecast for a past date would silently
    rewrite accuracy already measured, and two runs over the same window would disagree.
    """
    occupy(session, hotel, room, SETTLED)
    record_prediction(
        session,
        hotel,
        target_date=SETTLED,
        predicted=100.0,
        generated_at=moment(12),
        feature_digest="original",
    )
    before = evaluate(build_service(session), hotel, window_from=SETTLED, window_to=SETTLED)

    record_prediction(
        session,
        hotel,
        target_date=SETTLED,
        predicted=999.0,
        generated_at=moment(20),
        feature_digest="arrived-later",
    )
    after = evaluate(build_service(session), hotel, window_from=SETTLED, window_to=SETTLED)

    assert before.as_dict() == after.as_dict()
    assert after.by_model_version[0].within_calibration.scored_feature_digests == ("original",)


def test_a_tie_on_generated_at_is_broken_by_the_lowest_id(
    session: Session, hotel: Hotel, room: Room
) -> None:
    """Criterion 11. Two rows written at the same instant still resolve to one answer."""
    occupy(session, hotel, room, SETTLED)
    tie = moment(12)
    record_prediction(
        session,
        hotel,
        target_date=SETTLED,
        predicted=100.0,
        generated_at=tie,
        feature_digest="inserted-first",
    )
    record_prediction(
        session,
        hotel,
        target_date=SETTLED,
        predicted=500.0,
        generated_at=tie,
        feature_digest="inserted-second",
    )

    ids = list(
        session.execute(
            sa.text("SELECT feature_digest FROM demand_predictions ORDER BY id")
        ).scalars()
    )
    assert ids == ["inserted-first", "inserted-second"], "ids must ascend with insertion order"

    result = evaluate(build_service(session), hotel, window_from=SETTLED, window_to=SETTLED)

    assert result.by_model_version[0].within_calibration.scored_feature_digests == (
        "inserted-first",
    )


def test_the_selection_is_one_query_and_not_one_per_date(
    session: Session, hotel: Hotel, room: Room
) -> None:
    """No N+1: a window of ten dates costs the same one statement as a window of one."""
    for offset in range(10):
        day = SETTLED - dt.timedelta(days=offset)
        occupy(session, hotel, room, day)
        record_prediction(
            session,
            hotel,
            target_date=day,
            predicted=100.0,
            generated_at=moment(12),
            feature_digest=f"d{offset}",
        )

    statements: list[str] = []
    listener = lambda conn, cursor, statement, *rest: statements.append(statement)  # noqa: E731
    sa.event.listen(session.get_bind(), "before_cursor_execute", listener)
    try:
        evaluate(
            build_service(session),
            hotel,
            window_from=SETTLED - dt.timedelta(days=9),
            window_to=SETTLED,
        )
    finally:
        sa.event.remove(session.get_bind(), "before_cursor_execute", listener)

    selects = [s for s in statements if "FROM demand_predictions" in s]
    assert len(selects) == 1, selects


# ======================================================================================
# The settlement boundary, over real rows
# ======================================================================================


def test_a_target_date_exactly_at_the_boundary_is_scored(
    session: Session, hotel: Hotel, room: Room
) -> None:
    """Criterion 4."""
    occupy(session, hotel, room, SETTLED)
    record_prediction(
        session,
        hotel,
        target_date=SETTLED,
        predicted=100.0,
        generated_at=moment(12),
        feature_digest="boundary",
    )

    result = evaluate(build_service(session), hotel, window_from=SETTLED, window_to=SETTLED)

    assert result.ineligible_by_settlement == 0
    assert result.by_model_version[0].within_calibration.observations == 1


def test_a_target_date_one_day_short_is_not_scored(
    session: Session, hotel: Hotel, room: Room
) -> None:
    """Criterion 5."""
    day = SETTLED + dt.timedelta(days=1)
    occupy(session, hotel, room, day)
    record_prediction(
        session,
        hotel,
        target_date=day,
        predicted=100.0,
        generated_at=moment(12),
        feature_digest="too-soon",
    )

    result = evaluate(build_service(session), hotel, window_from=day, window_to=day)

    assert result.candidates == 1
    assert result.ineligible_by_settlement == 1
    assert result.by_model_version == ()


def test_a_confirmed_allocation_makes_the_result_provisional(
    session: Session, hotel: Hotel, room: Room
) -> None:
    """Criterion 8. ``confirmed`` can still become a no-show, so the night can still move."""
    occupy(session, hotel, room, SETTLED, status="confirmed")
    record_prediction(
        session,
        hotel,
        target_date=SETTLED,
        predicted=100.0,
        generated_at=moment(12),
        feature_digest="provisional",
    )

    result = evaluate(build_service(session), hotel, window_from=SETTLED, window_to=SETTLED)

    assert result.unsettled_allocations == 1
    assert result.settled is False


def test_a_checked_out_allocation_leaves_the_result_settled(
    session: Session, hotel: Hotel, room: Room
) -> None:
    """``checked_out`` is terminal, and ``checked_in`` leads only to it -- both are occupancy."""
    occupy(session, hotel, room, SETTLED, status="checked_out")
    record_prediction(
        session,
        hotel,
        target_date=SETTLED,
        predicted=100.0,
        generated_at=moment(12),
        feature_digest="final",
    )

    result = evaluate(build_service(session), hotel, window_from=SETTLED, window_to=SETTLED)

    assert result.unsettled_allocations == 0
    assert result.settled is True


def test_an_unsettled_allocation_outside_the_scored_window_is_not_counted(
    session: Session, hotel: Hotel, room: Room
) -> None:
    """A date nobody scored cannot make a measurement provisional."""
    occupy(session, hotel, room, SETTLED, status="checked_out")
    occupy(session, hotel, room, SETTLED + dt.timedelta(days=5), status="confirmed")
    record_prediction(
        session,
        hotel,
        target_date=SETTLED,
        predicted=100.0,
        generated_at=moment(12),
        feature_digest="scored",
    )

    result = evaluate(build_service(session), hotel, window_from=SETTLED, window_to=SETTLED)

    assert result.unsettled_allocations == 0
    assert result.settled is True


# ======================================================================================
# Ground truth
# ======================================================================================


def test_the_ground_truth_is_the_existing_occupancy_definition(
    session: Session, hotel: Hotel, room: Room
) -> None:
    """Criterion 25 over real rows: two occupied nights against a prediction of five."""
    room_type = session.execute(
        sa.select(RoomType).where(RoomType.hotel_id == hotel.id)
    ).scalar_one()
    second = make_room(session, hotel, room_type, number="102")
    session.commit()
    occupy(session, hotel, room, SETTLED)
    occupy(session, hotel, second, SETTLED)
    record_prediction(
        session,
        hotel,
        target_date=SETTLED,
        predicted=5.0,
        generated_at=moment(12),
        feature_digest="two-nights",
    )

    metrics = (
        evaluate(build_service(session), hotel, window_from=SETTLED, window_to=SETTLED)
        .by_model_version[0]
        .within_calibration.metrics
    )

    assert metrics.observations == 1
    assert metrics.mae == pytest.approx(3.0)


def test_a_cancelled_allocation_is_not_realised_demand(
    session: Session, hotel: Hotel, room: Room
) -> None:
    """``cancelled`` is outside ``OCCUPANCY_STATUSES``, so the date has no ground truth."""
    occupy(session, hotel, room, SETTLED, status="cancelled")
    record_prediction(
        session,
        hotel,
        target_date=SETTLED,
        predicted=100.0,
        generated_at=moment(12),
        feature_digest="no-truth",
    )

    segment = (
        evaluate(build_service(session), hotel, window_from=SETTLED, window_to=SETTLED)
        .by_model_version[0]
        .within_calibration
    )

    assert segment.observations == 0
    assert segment.skipped == 1
    assert segment.metrics.mae is None


# ======================================================================================
# Attribution and segmentation, over real rows
# ======================================================================================


def test_two_model_versions_are_reported_separately(
    session: Session, hotel: Hotel, room: Room
) -> None:
    """Criterion 19."""
    occupy(session, hotel, room, SETTLED)
    record_prediction(
        session,
        hotel,
        target_date=SETTLED,
        predicted=10.0,
        generated_at=moment(12),
        feature_digest="v1",
        model_version="demand_baseline_v1",
    )
    record_prediction(
        session,
        hotel,
        target_date=SETTLED,
        predicted=50.0,
        generated_at=moment(12),
        feature_digest="v2",
        model_version="demand_baseline_v2",
    )

    result = evaluate(build_service(session), hotel, window_from=SETTLED, window_to=SETTLED)

    assert [entry.model_version for entry in result.by_model_version] == [
        "demand_baseline_v1",
        "demand_baseline_v2",
    ]
    assert result.by_model_version[0].within_calibration.metrics.mae == pytest.approx(9.0)
    assert result.by_model_version[1].within_calibration.metrics.mae == pytest.approx(49.0)


def test_a_foreign_digest_is_reported_and_never_pooled(
    session: Session, hotel: Hotel, room: Room
) -> None:
    """Criterion 20."""
    occupy(session, hotel, room, SETTLED)
    record_prediction(
        session,
        hotel,
        target_date=SETTLED,
        predicted=100.0,
        generated_at=moment(12),
        feature_digest="foreign",
        digest="0" * 64,
    )

    result = evaluate(build_service(session), hotel, window_from=SETTLED, window_to=SETTLED)

    assert result.out_of_scope_model_digest == 1
    assert result.by_model_version == ()


def test_the_two_segments_are_reported_with_their_own_denominators(
    session: Session, hotel: Hotel, room: Room
) -> None:
    """Criteria 16 and 17, over real stored ``feature_values``."""
    earlier = SETTLED - dt.timedelta(days=1)
    occupy(session, hotel, room, SETTLED)
    occupy(session, hotel, room, earlier)
    record_prediction(
        session,
        hotel,
        target_date=SETTLED,
        predicted=165.0,
        generated_at=moment(12),
        feature_digest="small",
        features=SMALL_FEATURES,
    )
    record_prediction(
        session,
        hotel,
        target_date=earlier,
        predicted=3.0,
        generated_at=moment(12),
        feature_digest="large",
    )

    entry = evaluate(
        build_service(session), hotel, window_from=earlier, window_to=SETTLED
    ).by_model_version[0]

    assert entry.below_calibration.observations == 1
    assert entry.below_calibration.scored_feature_digests == ("small",)
    assert entry.below_calibration.metrics.mae == pytest.approx(164.0)
    assert entry.within_calibration.observations == 1
    assert entry.within_calibration.scored_feature_digests == ("large",)
    assert entry.within_calibration.metrics.mae == pytest.approx(2.0)


# ======================================================================================
# Tenant isolation
# ======================================================================================


def test_one_hotels_predictions_never_enter_anothers_evaluation(
    session: Session, engine: Engine, hotel: Hotel, room: Room
) -> None:
    """Criterion 22. Two hotels, the same dates, one table."""
    other, other_room = seed_hotel(session, slug="fa-other")
    grant_membership(engine, SUITE_EMAIL, str(other.public_id), "owner")

    occupy(session, hotel, room, SETTLED)
    occupy(session, other, other_room, SETTLED)
    record_prediction(
        session,
        hotel,
        target_date=SETTLED,
        predicted=10.0,
        generated_at=moment(12),
        feature_digest="mine",
    )
    record_prediction(
        session,
        other,
        target_date=SETTLED,
        predicted=900.0,
        generated_at=moment(12),
        feature_digest="theirs",
    )

    service = build_service(session)
    mine = evaluate(service, hotel, window_from=SETTLED, window_to=SETTLED)
    theirs = evaluate(service, other, window_from=SETTLED, window_to=SETTLED)

    assert mine.candidates == 1
    assert mine.by_model_version[0].within_calibration.scored_feature_digests == ("mine",)
    assert theirs.by_model_version[0].within_calibration.scored_feature_digests == ("theirs",)
    assert mine.hotel_public_id != theirs.hotel_public_id


def test_a_non_member_is_refused_before_anything_is_read(
    session: Session, engine: Engine, hotel: Hotel, room: Room
) -> None:
    """Criterion 23. The hotel's own 404, and no evaluation."""
    authenticated_client(engine, email=OTHER_EMAIL)
    occupy(session, hotel, room, SETTLED)
    record_prediction(
        session,
        hotel,
        target_date=SETTLED,
        predicted=100.0,
        generated_at=moment(12),
        feature_digest="hidden",
    )

    with pytest.raises(NotFoundError):
        evaluate(
            build_service(session, email=OTHER_EMAIL),
            hotel,
            window_from=SETTLED,
            window_to=SETTLED,
        )


def test_an_unknown_hotel_is_refused_the_same_way(session: Session, hotel: Hotel) -> None:
    """Indistinguishable from a hotel that exists and is not yours."""
    service = build_service(session)

    with pytest.raises(NotFoundError):
        service.evaluate(
            uuid.uuid4(),
            as_of_date=AS_OF,
            window_from=SETTLED,
            window_to=SETTLED,
        )


def test_the_repository_read_is_bounded_by_the_hotel(
    session: Session, hotel: Hotel, room: Room
) -> None:
    """Criterion 21, at the layer that enforces it."""
    other, _ = seed_hotel(session, slug="fa-bounded")
    record_prediction(
        session,
        other,
        target_date=SETTLED,
        predicted=100.0,
        generated_at=moment(12),
        feature_digest="not-mine",
    )

    rows = MlPredictionRepository(session).scorable_predictions(hotel.id, SETTLED, SETTLED)

    assert rows == []


# ======================================================================================
# The evaluation writes nothing
# ======================================================================================


def test_an_evaluation_leaves_the_database_byte_for_byte_unchanged(
    session: Session, hotel: Hotel, room: Room
) -> None:
    """Criteria 30 and 31. Counted and digested in the database, not inferred from the source."""
    occupy(session, hotel, room, SETTLED)
    record_prediction(
        session,
        hotel,
        target_date=SETTLED,
        predicted=100.0,
        generated_at=moment(12),
        feature_digest="untouched",
    )
    before = snapshot(session)

    evaluate(build_service(session), hotel, window_from=SETTLED, window_to=SETTLED)
    session.rollback()

    assert snapshot(session) == before


def test_two_evaluations_of_the_same_window_are_bit_identical(
    session: Session, hotel: Hotel, room: Room
) -> None:
    """Criterion 29, over real rows."""
    for offset in range(3):
        day = SETTLED - dt.timedelta(days=offset)
        occupy(session, hotel, room, day)
        record_prediction(
            session,
            hotel,
            target_date=day,
            predicted=100.0 + offset,
            generated_at=moment(12),
            feature_digest=f"day-{offset}",
        )

    service = build_service(session)
    window = {"window_from": SETTLED - dt.timedelta(days=2), "window_to": SETTLED}

    assert (
        evaluate(service, hotel, **window).as_dict() == evaluate(service, hotel, **window).as_dict()
    )


# ======================================================================================
# Contract and claims
# ======================================================================================


def test_the_result_reports_the_protocol_that_produced_it(
    session: Session, hotel: Hotel, room: Room
) -> None:
    """Criterion 48."""
    occupy(session, hotel, room, SETTLED)
    record_prediction(
        session,
        hotel,
        target_date=SETTLED,
        predicted=100.0,
        generated_at=moment(12),
        feature_digest="attributed",
    )

    result = evaluate(build_service(session), hotel, window_from=SETTLED, window_to=SETTLED)

    assert result.protocol_version == PROTOCOL.version
    assert result.protocol_checksum == PROTOCOL.checksum
    assert result.settlement_lag_days == 28
    assert result.as_of_date == AS_OF
    assert result.establishes_production_accuracy is False
    assert result.scored_from == SETTLED and result.scored_to == SETTLED


def test_the_alembic_head_is_still_the_stage_68_revision(session: Session) -> None:
    """Criterion 33. Stage 6.9 adds no migration."""
    revision = session.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()

    assert revision == "0013_llm_invocations"


def test_no_accuracy_table_was_created(session: Session) -> None:
    """Compute-and-return only: there is nowhere for a result to have been written."""
    tables = list(
        session.execute(
            sa.text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public' "
                "AND (table_name LIKE '%accuracy%' OR table_name LIKE '%evaluation%' "
                "OR table_name LIKE '%outcome%')"
            )
        ).scalars()
    )

    assert tables == []


# ======================================================================================
# The evaluation event
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
    logger = logging.getLogger("app.services.ml_accuracy")
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


#: Field names on a record that this stage did not put there: the standard library's own, read
#: from a real record, plus the three the shared console handler adds once logging is configured.
STDLIB_RECORD_FIELDS = frozenset(
    logging.LogRecord("n", logging.INFO, "p", 1, "m", None, None).__dict__
) | {"message", "asctime", "request_id"}


def field(record: logging.LogRecord, name: str) -> Any:
    return record.__dict__[name]


def test_one_evaluation_emits_one_event_naming_nothing_sensitive(
    session: Session, hotel: Hotel, room: Room, events: RecordingHandler
) -> None:
    """Criteria 45, 46 and 47 against a real evaluation over real rows."""
    occupy(session, hotel, room, SETTLED)
    record_prediction(
        session,
        hotel,
        target_date=SETTLED,
        predicted=137.5,
        generated_at=moment(12),
        feature_digest="e" * 64,
    )

    evaluate(build_service(session), hotel, window_from=SETTLED, window_to=SETTLED)

    emitted = [record for record in events.records if hasattr(record, "outcome")]
    assert len(emitted) == 1
    assert field(emitted[0], "outcome") == "evaluated"
    assert field(emitted[0], "predictions_scored") == 1

    # The internal id is checked structurally rather than as a substring: it is a small integer
    # here, so `str(hotel.id)` would match the digit in a legitimate count and the assertion
    # would fail for a reason that is not a leak. What actually rules it out is that the five
    # attached fields are named, and none of them is an identifier.
    attached = {
        name: value
        for name, value in emitted[0].__dict__.items()
        if name not in STDLIB_RECORD_FIELDS
    }
    assert set(attached) == {
        "outcome",
        "model_version",
        "predictions_scored",
        "predictions_skipped",
        "duration_ms",
    }

    rendered = emitted[0].getMessage() + repr(attached)
    for forbidden in (
        "137.5",
        "e" * 64,
        APPROVED_DIGEST,
        str(hotel.public_id),
        "hotel_id",
        "demand_lag_7",
        "SELECT",
    ):
        assert forbidden not in rendered, forbidden
