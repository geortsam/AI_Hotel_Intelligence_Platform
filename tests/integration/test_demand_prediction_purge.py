"""Demand prediction retention and hotel deletion (Issue H8), over real PostgreSQL.

A stored prediction is kept while its ``target_date`` is no more than
`demand_prediction_retention_days` (730 by default) before its hotel's own today, and is then
physically deleted by `purge_expired_predictions(session, settings)` --
`python -m app.jobs.purge_demand_predictions` -- one hotel and one bounded batch at a time. A
hotel's delete removes its predictions in the same transaction, and a refused delete restores
them; a direct SQL delete is still refused by ``ON DELETE RESTRICT``.

These tests pin: the cutoff day and the hotel's calendar, the configured period, every hotel and
tenant scope, empty and clean tables, repetition, batching, failure and retry, that accuracy's
selection over every retained window is unchanged (even mid-purge), hotel deletion and its
rollback, the RESTRICT key, the 409's wording, independence from every other table, and the
command.

Predictions are written directly with chosen target dates and generation times: the
application only ever writes one for a date it can forecast, and never back-dates one.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.jobs.purge_demand_predictions import main
from app.ml import artifact_store
from app.ml.artifact_store import ArtifactLocation
from app.models.guest import Guest
from app.models.hotel import Hotel
from app.repositories.ml_prediction import MlPredictionRepository
from app.repositories.ml_prediction_removal import MlPredictionRemovalRepository
from app.services.demand_prediction_retention import (
    PredictionPurgeResult,
    purge_expired_predictions,
)
from app.services.hotel import UNDELETABLE_HOTEL_MESSAGE
from tests.artifact_builder import build_artifact
from tests.integration.conftest import (
    TEST_DATABASE_URL,
    authenticated_client,
    grant_membership,
    make_hotel,
    observe,
    requires_postgres,
)

pytestmark = requires_postgres

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

#: Noon UTC: the same calendar date in Athens (UTC+3) and in UTC.
NOON = dt.datetime(2026, 10, 9, 12, tzinfo=dt.UTC)
TODAY = NOON.date()
#: The default 730 days before TODAY: the oldest target date kept.
CUTOFF = dt.date(2024, 10, 9)

OUTPUT = re.compile(
    r"^demand prediction purge: (\d+) expired prediction\(s\) deleted at (\d+) hotel\(s\) "
    r"in (\d+) batch\(es\); retention (\d+) day\(s\)$"
)
RESTRICT_VIOLATION = "23001"
HOTEL_KEY = "fk_demand_predictions_hotel_id_hotels"

INSERT = sa.text(
    "INSERT INTO demand_predictions (hotel_id, target_date, forecast_horizon_days, "
    "prediction_cutoff, predicted_room_nights, model_name, model_version, feature_version, "
    "dataset_version, canonical_model_digest, feature_values, feature_digest, request_id, "
    "generated_at) VALUES (:hotel, :target, :horizon, :cutoff, :value, 'demand', :version, "
    "'f1', 'd1', 'digest', CAST(:features AS jsonb), :digest, NULL, :generated) "
    "RETURNING id"
)


def predict(
    session: Session,
    hotel: Hotel,
    target: dt.date,
    *,
    generated: dt.datetime | None = None,
    horizon: int = 7,
    version: str = "v1",
    value: float = 10.0,
) -> int:
    """One committed prediction for *target*. A fresh digest each time, so repeats are rows."""
    generated = generated or dt.datetime.combine(target, dt.time(9), tzinfo=dt.UTC)
    prediction_id = session.execute(
        INSERT,
        {
            "hotel": hotel.id,
            "target": target,
            "horizon": horizon,
            "cutoff": dt.datetime.combine(target, dt.time(0), tzinfo=dt.UTC)
            - dt.timedelta(days=horizon),
            "value": value,
            "version": version,
            "features": '{"demand_lag_7": 1}',
            "digest": uuid.uuid4().hex,
            "generated": generated,
        },
    ).scalar_one()
    session.commit()
    return int(prediction_id)


def settings(retention_days: int = 730, **overrides: Any) -> Settings:
    return Settings(
        environment="test",
        database_url=TEST_DATABASE_URL,
        demand_prediction_retention_days=retention_days,
        **overrides,
    )


def purge(
    session: Session, retention_days: int = 730, *, at: dt.datetime = NOON, **kwargs: Any
) -> PredictionPurgeResult:
    return purge_expired_predictions(session, settings(retention_days), clock=lambda: at, **kwargs)


def ids(session: Session, hotel: Hotel | None = None) -> set[int]:
    query = "SELECT id FROM demand_predictions"
    if hotel is None:
        return set(session.execute(sa.text(query)).scalars())
    return set(session.execute(sa.text(query + " WHERE hotel_id = :h"), {"h": hotel.id}).scalars())


def public_id(session: Session, prediction_id: int) -> str:
    """A prediction's public UUID: the identifier that could leak, unlike its small int id."""
    return str(
        session.execute(
            sa.text("SELECT public_id FROM demand_predictions WHERE id = :i"), {"i": prediction_id}
        ).scalar_one()
    )


def snapshot(session: Session) -> list[tuple[Any, ...]]:
    """Every column of every row, in id order: what "left exactly as it was" means."""
    return [
        tuple(row)
        for row in session.execute(sa.text("SELECT * FROM demand_predictions ORDER BY id"))
    ]


@pytest.fixture
def athens(session: Session) -> Hotel:
    hotel = make_hotel(session, slug="h8-athens")
    session.commit()
    return hotel


@pytest.fixture
def other(session: Session) -> Hotel:
    hotel = make_hotel(session, slug="h8-other")
    hotel.timezone = "UTC"
    session.commit()
    return hotel


# ======================================================================================
# A. What expires: target date against the hotel's own today
# ======================================================================================


def test_a_target_date_one_day_before_the_cutoff_is_deleted_and_the_cutoff_day_is_kept(
    session: Session, athens: Hotel
) -> None:
    before = predict(session, athens, CUTOFF - dt.timedelta(days=1))
    on = predict(session, athens, CUTOFF)
    recent = predict(session, athens, TODAY)
    future = predict(session, athens, TODAY + dt.timedelta(days=7))

    result = purge(session)

    assert (result.predictions_deleted, result.hotels, result.batches) == (1, 1, 1)
    assert ids(session) == {on, recent, future}
    assert before not in ids(session)


def test_generation_time_never_decides(session: Session, athens: Hotel) -> None:
    """A prediction generated long ago for a retained date stays; one generated today for an
    expired date goes."""
    old_but_retained = predict(
        session, athens, CUTOFF, generated=dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
    )
    new_but_expired = predict(session, athens, CUTOFF - dt.timedelta(days=1), generated=NOON)

    purge(session)

    assert ids(session) == {old_but_retained}
    assert new_but_expired not in ids(session)


@pytest.mark.parametrize(
    ("zone", "kept_from"),
    [
        # 22:30 UTC on 10-09 is already 10-10 in Athens: its cutoff moves a day later.
        ("Europe/Athens", dt.date(2024, 10, 10)),
        ("UTC", dt.date(2024, 10, 9)),
        # A zone PostgreSQL and Python do not know: UTC, as every hotel "today" falls back to.
        ("Mars/Olympus", dt.date(2024, 10, 9)),
    ],
)
def test_the_cutoff_is_taken_in_the_hotels_own_calendar(
    session: Session, athens: Hotel, zone: str, kept_from: dt.date
) -> None:
    athens.timezone = zone
    session.commit()
    edge = predict(session, athens, dt.date(2024, 10, 9))

    purge(session, at=dt.datetime(2026, 10, 9, 22, 30, tzinfo=dt.UTC))

    assert (edge in ids(session)) == (kept_from <= dt.date(2024, 10, 9))


@pytest.mark.parametrize("zone", ["UTC", "Pacific/Kiritimati", "Pacific/Pago_Pago"])
def test_the_database_sessions_time_zone_cannot_move_the_cutoff(athens: Hotel, zone: str) -> None:
    """The cutoff is a date computed from the hotel's zone and handed to SQL as a date.

    The zone is set on every connection, because each commit hands a pooled connection back."""
    assert TEST_DATABASE_URL is not None
    zoned = sa.create_engine(
        TEST_DATABASE_URL,
        poolclass=sa.pool.NullPool,
        connect_args={"options": f"-c TimeZone={zone}"},
    )
    try:
        with Session(zoned, expire_on_commit=False) as session:
            assert session.execute(sa.text("SHOW TimeZone")).scalar_one() == zone
            hotel = session.get(Hotel, athens.id)
            assert hotel is not None
            before = predict(session, hotel, CUTOFF - dt.timedelta(days=1))
            on = predict(session, hotel, CUTOFF)

            purge(session)

            assert ids(session) == {on}
            assert before not in ids(session)
    finally:
        zoned.dispose()


# ======================================================================================
# B. The configured period
# ======================================================================================


def test_the_default_period_is_730_days(session: Session, athens: Hotel) -> None:
    predict(session, athens, TODAY - dt.timedelta(days=731))
    kept = predict(session, athens, TODAY - dt.timedelta(days=730))

    result = purge_expired_predictions(
        session, Settings(environment="test", database_url=TEST_DATABASE_URL), clock=lambda: NOON
    )

    assert (result.predictions_deleted, result.retention_days) == (1, 730)
    assert ids(session) == {kept}


def test_the_configured_period_decides_what_is_deleted(session: Session, athens: Hotel) -> None:
    hundred = predict(session, athens, TODAY - dt.timedelta(days=100))
    forty = predict(session, athens, TODAY - dt.timedelta(days=40))

    assert purge(session, 730).predictions_deleted == 0
    assert purge(session, 100).predictions_deleted == 0
    assert ids(session) == {hundred, forty}

    result = purge(session, 60)
    assert (result.predictions_deleted, result.retention_days) == (1, 60)
    assert ids(session) == {forty}

    assert purge(session, 28).predictions_deleted == 1
    assert ids(session) == set()


# ======================================================================================
# C. Every hotel, one hotel per statement
# ======================================================================================


def test_expired_predictions_at_every_hotel_are_deleted(
    session: Session, athens: Hotel, other: Hotel
) -> None:
    predict(session, athens, CUTOFF - dt.timedelta(days=5))
    predict(session, other, CUTOFF - dt.timedelta(days=1))
    kept_a = predict(session, athens, CUTOFF)
    kept_b = predict(session, other, TODAY)

    result = purge(session)

    assert (result.predictions_deleted, result.hotels, result.batches) == (2, 2, 2)
    assert ids(session) == {kept_a, kept_b}


def test_one_hotels_purge_never_touches_another_hotel(
    session: Session, athens: Hotel, other: Hotel
) -> None:
    predict(session, athens, CUTOFF - dt.timedelta(days=1))
    elsewhere = predict(session, other, CUTOFF - dt.timedelta(days=1))

    deleted = MlPredictionRemovalRepository(session).purge_expired(athens.id, CUTOFF, limit=10)
    session.commit()

    assert deleted == 1
    assert ids(session) == {elsewhere}


def test_a_hotel_with_nothing_expired_is_not_counted(
    session: Session, athens: Hotel, other: Hotel
) -> None:
    predict(session, athens, CUTOFF - dt.timedelta(days=1))
    predict(session, other, CUTOFF)

    result = purge(session)

    assert (result.predictions_deleted, result.hotels, result.batches) == (1, 1, 1)


# ======================================================================================
# D. Empty, already clean, repeated
# ======================================================================================


def test_an_empty_table_is_a_successful_purge_of_nothing(session: Session, athens: Hotel) -> None:
    assert purge(session) == PredictionPurgeResult(0, 0, 0, 730)


def test_a_table_with_nothing_expired_is_left_exactly_as_it_was(
    session: Session, athens: Hotel, other: Hotel
) -> None:
    for target in (CUTOFF, TODAY, TODAY + dt.timedelta(days=7)):
        predict(session, athens, target)
        predict(session, other, target)
    before = snapshot(session)

    assert purge(session).predictions_deleted == 0

    assert snapshot(session) == before


def test_running_the_purge_again_is_safe_and_finds_nothing(
    session: Session, athens: Hotel, other: Hotel
) -> None:
    for days in (1, 2, 3):
        predict(session, athens, CUTOFF - dt.timedelta(days=days))
        predict(session, other, CUTOFF - dt.timedelta(days=days))
    predict(session, athens, CUTOFF)

    first = purge(session)
    after_first = snapshot(session)
    second = purge(session)

    assert (first.predictions_deleted, first.hotels) == (6, 2)
    assert second == PredictionPurgeResult(0, 0, 0, 730)
    assert snapshot(session) == after_first


# ======================================================================================
# E. Bounded batches, failure and retry
# ======================================================================================


def test_more_than_one_batch_is_processed_to_completion_at_every_hotel(
    session: Session, athens: Hotel, other: Hotel
) -> None:
    for days in range(1, 6):
        predict(session, athens, CUTOFF - dt.timedelta(days=days))
    for days in range(1, 4):
        predict(session, other, CUTOFF - dt.timedelta(days=days))

    result = purge(session, batch_size=2)

    # 5 -> 2 + 2 + 1; 3 -> 2 + 1.
    assert (result.predictions_deleted, result.hotels, result.batches) == (8, 2, 5)
    assert ids(session) == set()


def test_a_batch_deletes_the_oldest_target_dates_first(session: Session, athens: Hotel) -> None:
    oldest = predict(session, athens, CUTOFF - dt.timedelta(days=30))
    middle = predict(session, athens, CUTOFF - dt.timedelta(days=20))
    newest = predict(session, athens, CUTOFF - dt.timedelta(days=10))

    MlPredictionRemovalRepository(session).purge_expired(athens.id, CUTOFF, limit=1)
    session.commit()

    assert ids(session) == {middle, newest}
    assert oldest not in ids(session)


def test_a_failed_purge_keeps_every_completed_batch_and_a_retry_finishes(
    session: Session, athens: Hotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    for days in range(1, 6):
        predict(session, athens, CUTOFF - dt.timedelta(days=days))
    kept = predict(session, athens, CUTOFF)
    original = MlPredictionRemovalRepository.purge_expired
    calls = {"n": 0}

    def flaky(self: MlPredictionRemovalRepository, *args: Any, **kwargs: Any) -> int:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("connection lost")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MlPredictionRemovalRepository, "purge_expired", flaky)
    with pytest.raises(RuntimeError, match="connection lost"):
        purge(session, batch_size=2)

    assert len(ids(session)) == 6 - 2  # the first batch is committed and stays deleted

    monkeypatch.setattr(MlPredictionRemovalRepository, "purge_expired", original)
    result = purge(session, batch_size=2)
    assert (result.predictions_deleted, result.batches) == (3, 2)
    assert ids(session) == {kept}


# ======================================================================================
# F. Accuracy's selection over every retained window is unchanged
# ======================================================================================


def scorable(session: Session, hotel: Hotel, date_from: dt.date, date_to: dt.date) -> list[Any]:
    return MlPredictionRepository(session).scorable_predictions(hotel.id, date_from, date_to)


def test_the_earliest_prediction_of_every_retained_group_is_what_accuracy_still_selects(
    session: Session, athens: Hotel
) -> None:
    """Several predictions per (target date, horizon, model version), on both sides of the
    cutoff, in two model versions. After the purge every retained row is byte-identical and
    the accuracy selection over the retained window is exactly what it was."""
    for target in (
        CUTOFF - dt.timedelta(days=2),
        CUTOFF - dt.timedelta(days=1),
        CUTOFF,
        CUTOFF + dt.timedelta(days=1),
        TODAY - dt.timedelta(days=40),
    ):
        for version in ("v1", "v2"):
            for hour, value in ((15, 3.0), (9, 1.0), (12, 2.0)):
                predict(
                    session,
                    athens,
                    target,
                    version=version,
                    value=value,
                    generated=dt.datetime.combine(target, dt.time(hour), tzinfo=dt.UTC),
                )
    window = (CUTOFF, TODAY)
    selected_before = scorable(session, athens, *window)
    retained_before = [row for row in snapshot(session) if row[2] >= CUTOFF]

    result = purge(session)

    assert result.predictions_deleted == 2 * 2 * 3
    assert scorable(session, athens, *window) == selected_before
    assert [row for row in snapshot(session) if row[2] >= CUTOFF] == retained_before
    assert snapshot(session) == retained_before
    # The earliest of each group is the 09:00 prediction, valued 1.0.
    assert {candidate.predicted_room_nights for candidate in selected_before} == {1.0}
    assert scorable(session, athens, dt.date(2000, 1, 1), CUTOFF - dt.timedelta(days=1)) == []


def test_an_interrupted_purge_never_changes_which_prediction_accuracy_selects(
    session: Session, athens: Hotel
) -> None:
    """Within one target date the newest prediction goes first, so until the group is gone
    its earliest prediction -- the one accuracy selects -- is still there."""
    target = CUTOFF - dt.timedelta(days=1)
    for hour, value in ((15, 3.0), (9, 1.0), (12, 2.0)):
        predict(
            session,
            athens,
            target,
            value=value,
            generated=dt.datetime.combine(target, dt.time(hour), tzinfo=dt.UTC),
        )
    repository = MlPredictionRemovalRepository(session)
    [selected] = scorable(session, athens, target, target)
    assert selected.predicted_room_nights == 1.0

    for remaining in (2, 1):
        repository.purge_expired(athens.id, CUTOFF, limit=1)
        session.commit()
        assert len(ids(session)) == remaining
        assert scorable(session, athens, target, target) == [selected]

    repository.purge_expired(athens.id, CUTOFF, limit=1)
    session.commit()
    assert scorable(session, athens, target, target) == []


def test_a_purged_window_reads_as_empty_through_the_read_api(
    engine: Engine, session: Session, athens: Hotel
) -> None:
    """The documented consequence: nothing distinguishes a purged window from an unused one."""
    predict(session, athens, CUTOFF - dt.timedelta(days=1))
    kept = CUTOFF
    predict(session, athens, kept)
    client = authenticated_client(engine, email="h8-reader@example.test")
    grant_membership(engine, "h8-reader@example.test", str(athens.public_id), "viewer")
    url = f"/api/v1/hotels/{athens.public_id}/ml/demand-predictions"
    window = {"date_from": "2024-01-01", "date_to": "2024-12-31"}
    assert client.get(url, params=window).json()["total"] == 2

    purge(session)

    body = client.get(url, params=window).json()
    assert body["total"] == 1
    assert [item["target_date"] for item in body["items"]] == [kept.isoformat()]
    expired_window = {"date_from": "2024-01-01", "date_to": "2024-10-08"}
    response = client.get(url, params=expired_window)
    assert response.status_code == 200
    assert response.json()["total"] == 0


# ======================================================================================
# G. Hotel deletion
# ======================================================================================


@pytest.fixture
def owner(engine: Engine, athens: Hotel, other: Hotel) -> Any:
    client = authenticated_client(engine, email="h8-owner@example.test")
    for hotel in (athens, other):
        grant_membership(engine, "h8-owner@example.test", str(hotel.public_id), "owner")
    return client


def hotel_exists(session: Session, hotel: Hotel) -> bool:
    return bool(
        session.execute(sa.text("SELECT 1 FROM hotels WHERE id = :h"), {"h": hotel.id}).first()
    )


def test_a_hotel_whose_only_dependents_are_predictions_is_deleted_with_them(
    session: Session, owner: Any, athens: Hotel, other: Hotel
) -> None:
    for target in (CUTOFF - dt.timedelta(days=1), TODAY, TODAY + dt.timedelta(days=7)):
        predict(session, athens, target)
    elsewhere = predict(session, other, TODAY)

    response = owner.delete(f"/api/v1/hotels/{athens.public_id}")

    assert response.status_code == 204, response.text
    assert not hotel_exists(session, athens)
    assert ids(session, athens) == set()
    assert ids(session) == {elsewhere}
    assert hotel_exists(session, other)


def test_a_refused_delete_restores_every_prediction(
    session: Session, owner: Any, athens: Hotel
) -> None:
    for target in (CUTOFF, TODAY, TODAY + dt.timedelta(days=7)):
        predict(session, athens, target)
    session.add(Guest(hotel_id=athens.id, first_name="Ada", last_name="Lovelace"))
    session.commit()
    before = snapshot(session)
    memberships = session.execute(
        sa.text("SELECT count(*) FROM user_hotels WHERE hotel_id = :h"), {"h": athens.id}
    ).scalar_one()

    response = owner.delete(f"/api/v1/hotels/{athens.public_id}")

    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "CONFLICT",
        "message": UNDELETABLE_HOTEL_MESSAGE,
        "details": [],
    }
    assert hotel_exists(session, athens)
    assert snapshot(session) == before
    assert (
        session.execute(
            sa.text("SELECT count(*) FROM user_hotels WHERE hotel_id = :h"), {"h": athens.id}
        ).scalar_one()
        == memberships
    )
    # And the hotel is still fully usable by its owner.
    assert owner.get(f"/api/v1/hotels/{athens.public_id}").status_code == 200


def test_the_refusal_is_safe_and_no_longer_lists_six_kinds_of_record(
    session: Session, owner: Any, athens: Hotel
) -> None:
    predict(session, athens, TODAY)
    session.add(Guest(hotel_id=athens.id, first_name="Ada", last_name="Lovelace"))
    session.commit()

    text = owner.delete(f"/api/v1/hotels/{athens.public_id}").text.lower()

    assert "cannot be deleted" in text and "deactivate the hotel" in text
    assert "room types, rooms, guests, bookings, revenue and expenses" not in text
    for leak in (
        "fk_",
        "demand_predictions",
        "guests_hotel_id",
        "delete from",
        "psycopg",
        "sqlalchemy",
        "restrict",
        "traceback",
        "key (id)",
        "ada",
    ):
        assert leak not in text, leak


def test_a_direct_sql_delete_is_still_refused_by_the_restrict_key(
    session: Session, athens: Hotel
) -> None:
    predict(session, athens, TODAY)

    with pytest.raises(IntegrityError) as refused:
        session.execute(sa.text("DELETE FROM hotels WHERE id = :h"), {"h": athens.id})
    session.rollback()

    assert getattr(refused.value.orig, "sqlstate", None) == RESTRICT_VIOLATION
    assert refused.value.orig.diag.constraint_name == HOTEL_KEY  # type: ignore[union-attr]
    assert hotel_exists(session, athens)
    assert len(ids(session, athens)) == 1


def test_the_foreign_key_is_still_restrict(session: Session) -> None:
    rule = session.execute(
        sa.text("SELECT confdeltype FROM pg_constraint WHERE conname = :name"),
        {"name": HOTEL_KEY},
    ).scalar_one()
    assert rule == "r"


@pytest.fixture
def serving_artifact(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ArtifactLocation]:
    location = build_artifact(tmp_path_factory.mktemp("h8-artifact"))
    artifact_store.configure(location)
    yield location
    artifact_store.configure(None)


def test_a_viewers_forecast_no_longer_makes_the_hotel_undeletable(
    engine: Engine,
    session: Session,
    owner: Any,
    athens: Hotel,
    serving_artifact: ArtifactLocation,
) -> None:
    """The audit's reproduction, end to end: a hotel with only a declared observation span,
    one forecast read by a viewer, then the owner's delete."""
    target = dt.date(2026, 6, 1)
    observe(session, athens, target - dt.timedelta(days=28), target)
    session.commit()
    viewer = authenticated_client(engine, email="h8-viewer@example.test")
    grant_membership(engine, "h8-viewer@example.test", str(athens.public_id), "viewer")
    forecast = viewer.get(
        f"/api/v1/hotels/{athens.public_id}/ml/demand-forecast",
        params={"target_date": str(target)},
    )
    assert forecast.status_code == 200, forecast.text
    assert len(ids(session, athens)) == 1

    response = owner.delete(f"/api/v1/hotels/{athens.public_id}")

    assert response.status_code == 204, response.text
    assert not hotel_exists(session, athens)
    assert ids(session) == set()


# ======================================================================================
# H. The purge touches nothing else
# ======================================================================================


COUNTED = (
    "hotels",
    "users",
    "user_hotels",
    "guests",
    "audit_events",
    "llm_invocations",
    "demand_observation_periods",
)


def test_the_purge_touches_no_other_table(engine: Engine, session: Session, athens: Hotel) -> None:
    authenticated_client(engine, email="h8-bystander@example.test")
    grant_membership(engine, "h8-bystander@example.test", str(athens.public_id), "owner")
    observe(session, athens, CUTOFF - dt.timedelta(days=60), CUTOFF - dt.timedelta(days=1))
    session.add(Guest(hotel_id=athens.id, first_name="Ada", last_name="Lovelace"))
    predict(session, athens, CUTOFF - dt.timedelta(days=1))
    session.commit()

    def counts() -> dict[str, int]:
        return {
            table: int(session.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one())
            for table in COUNTED
        }

    before = counts()
    assert purge(session).predictions_deleted == 1
    assert counts() == before


# ======================================================================================
# I. The command
# ======================================================================================


def test_the_command_output_is_counts_only(
    session: Session, athens: Hotel, capsys: pytest.CaptureFixture[str]
) -> None:
    today = dt.datetime.now(dt.UTC).date()
    expired = predict(session, athens, today - dt.timedelta(days=800))
    live = predict(session, athens, today)
    expired_public = public_id(session, expired)

    assert main([], settings=settings()) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    match = OUTPUT.match(captured.out.strip())
    assert match is not None, captured.out
    assert match.groups() == ("1", "1", "1", "730")
    assert ids(session) == {live}
    for secret in (expired_public, str(athens.public_id), athens.slug, "Europe/Athens"):
        assert secret not in captured.out
    assert TEST_DATABASE_URL is not None and TEST_DATABASE_URL not in captured.out


def run_command(
    tmp_path: Path, database_url: str, *arguments: str, **extra_env: str
) -> subprocess.CompletedProcess[str]:
    """``python -m app.jobs.purge_demand_predictions`` as an operator runs it, in a fresh process.

    Run from an empty directory so no ``.env`` is read, with every ``POSTGRES_*`` and retention
    variable removed and ``DATABASE_URL`` set explicitly.
    """
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("POSTGRES_", "DATABASE_URL", "DEMAND_PREDICTION_"))
    }
    environment["DATABASE_URL"] = database_url
    environment["PYTHONPATH"] = str(REPOSITORY_ROOT / "backend")
    environment.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "app.jobs.purge_demand_predictions", *arguments],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_module_command_purges_and_exits_zero(
    session: Session, athens: Hotel, other: Hotel, tmp_path: Path
) -> None:
    today = dt.datetime.now(dt.UTC).date()
    expired = predict(session, athens, today - dt.timedelta(days=800))
    elsewhere = predict(session, other, today - dt.timedelta(days=800))
    live = predict(session, athens, today - dt.timedelta(days=700))
    secrets = [public_id(session, expired), public_id(session, elsewhere), str(athens.public_id)]
    assert TEST_DATABASE_URL is not None

    completed = run_command(tmp_path, TEST_DATABASE_URL)

    assert completed.returncode == 0, completed.stderr
    match = OUTPUT.match(completed.stdout.strip())
    assert match is not None, completed.stdout
    assert match.groups() == ("2", "2", "2", "730")
    assert ids(session) == {live}
    for secret in (*secrets, TEST_DATABASE_URL):
        assert secret not in completed.stdout
        assert secret not in completed.stderr


def test_the_module_command_reads_the_retention_from_the_environment(
    session: Session, athens: Hotel, tmp_path: Path
) -> None:
    today = dt.datetime.now(dt.UTC).date()
    predict(session, athens, today - dt.timedelta(days=100))
    kept = predict(session, athens, today - dt.timedelta(days=20))
    assert TEST_DATABASE_URL is not None

    completed = run_command(tmp_path, TEST_DATABASE_URL, DEMAND_PREDICTION_RETENTION_DAYS="60")

    assert completed.returncode == 0, completed.stderr
    match = OUTPUT.match(completed.stdout.strip())
    assert match is not None and match.groups() == ("1", "1", "1", "60"), completed.stdout
    assert ids(session) == {kept}


def test_the_module_command_refuses_an_invalid_retention_without_deleting(
    session: Session, athens: Hotel, tmp_path: Path
) -> None:
    """Shorter than the settlement lag: a configuration error, exit 1, nothing deleted."""
    expired = predict(session, athens, dt.date(2020, 1, 1))
    assert TEST_DATABASE_URL is not None

    completed = run_command(tmp_path, TEST_DATABASE_URL, DEMAND_PREDICTION_RETENTION_DAYS="27")

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "FAILED (ValidationError)" in completed.stderr
    assert ids(session) == {expired}


def test_the_module_command_exits_non_zero_without_leaking_the_connection(tmp_path: Path) -> None:
    unreachable = (
        "postgresql+psycopg://purge-user:PASSWORD-SENTINEL@127.0.0.1:1/nowhere?connect_timeout=3"
    )

    completed = run_command(tmp_path, unreachable)

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "FAILED (OperationalError)" in completed.stderr
    for secret in ("PASSWORD-SENTINEL", "purge-user", "nowhere", "Traceback"):
        assert secret not in completed.stderr


def test_the_module_command_refuses_an_unknown_argument(tmp_path: Path) -> None:
    unused = "postgresql+psycopg://nobody:unused@127.0.0.1:1/unused?connect_timeout=1"
    completed = run_command(tmp_path, unused, "--hotel", "anything")
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "unrecognized arguments" in completed.stderr
