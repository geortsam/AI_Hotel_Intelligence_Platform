"""Static and in-memory guards on demand prediction retention and hotel deletion (Issue H8).

`tests/integration/test_demand_prediction_purge.py` proves the purge and the hotel delete over real
PostgreSQL. This file proves what needs no database: the setting's default and bounds, the cutoff
arithmetic, per-hotel calendars, batching and its transactions, count-only reporting, which
modules may delete a prediction, that no HTTP surface reaches the purge, and the order and
wording of the hotel delete.
"""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import logging
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from annotated_types import Ge
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

import app.services.demand_prediction_retention as retention_module
from app.api import deps
from app.core.config import Settings
from app.core.errors import ConflictError
from app.ml.accuracy_protocol import SETTLEMENT_LAG_DAYS
from app.repositories.ml_prediction_removal import MlPredictionRemovalRepository
from app.services.demand_prediction_retention import (
    PURGE_BATCH,
    PredictionPurgeResult,
    purge_expired_predictions,
    retention_cutoff,
)
from app.services.hotel import UNDELETABLE_HOTEL_MESSAGE, HotelService
from tests.backend.test_retention_layering import APP, sources

#: 12:00 UTC on 2026-10-09: the same calendar date in every zone from UTC-11 to UTC+11.
NOON = dt.datetime(2026, 10, 9, 12, tzinfo=dt.UTC)


def settings(**overrides: Any) -> Settings:
    return Settings(environment="test", **overrides)


# ======================================================================================
# The setting
# ======================================================================================


def test_the_default_retention_is_730_days() -> None:
    assert settings().demand_prediction_retention_days == 730


def test_the_retention_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMAND_PREDICTION_RETENTION_DAYS", "400")
    assert settings().demand_prediction_retention_days == 400


@pytest.mark.parametrize("days", [-1, 0, 1, 27, 3651])
def test_the_retention_is_bounded_at_both_ends(days: int) -> None:
    with pytest.raises(ValidationError, match="demand_prediction_retention_days"):
        settings(demand_prediction_retention_days=days)


@pytest.mark.parametrize("days", [28, 3650])
def test_the_retention_bounds_themselves_are_accepted(days: int) -> None:
    assert settings(demand_prediction_retention_days=days).demand_prediction_retention_days == days


def test_the_lower_bound_is_the_accuracy_protocols_settlement_lag() -> None:
    """Shorter would delete a prediction before its first scorable day."""
    field = Settings.model_fields["demand_prediction_retention_days"]
    [lower] = [item.ge for item in field.metadata if isinstance(item, Ge)]
    assert lower == SETTLEMENT_LAG_DAYS


def test_the_default_keeps_a_full_performance_window_and_its_year_earlier_baseline() -> None:
    """Forecast performance allows a 366-day window and a baseline a year before it."""
    from app.services.ml_performance import MAX_WINDOW_DAYS

    assert settings().demand_prediction_retention_days >= MAX_WINDOW_DAYS + 365 - 1


# ======================================================================================
# The cutoff
# ======================================================================================


@pytest.mark.parametrize(
    ("today", "days", "cutoff"),
    [
        (dt.date(2026, 10, 9), 730, dt.date(2024, 10, 9)),
        (dt.date(2026, 10, 9), 28, dt.date(2026, 9, 11)),
        # Calendar days, so a leap day inside the period is one of them.
        (dt.date(2024, 3, 1), 365, dt.date(2023, 3, 2)),
        (dt.date(2025, 3, 1), 365, dt.date(2024, 3, 1)),
    ],
)
def test_the_cutoff_is_today_minus_the_retention_in_calendar_days(
    today: dt.date, days: int, cutoff: dt.date
) -> None:
    assert retention_cutoff(today, days) == cutoff


# ======================================================================================
# The job, over an in-memory repository
# ======================================================================================


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class FakeRemoval:
    """Hotels and their expired-row counts; records every call the job makes."""

    def __init__(self, hotels: list[tuple[Any, dt.date, int]], fail_on_call: int | None = None):
        self.hotels = hotels
        self.left = {hotel.id: expired for hotel, _, expired in hotels}
        self.calls: list[tuple[int, dt.date, int]] = []
        self.fail_on_call = fail_on_call

    def hotels_with_predictions(self) -> list[tuple[Any, dt.date]]:
        return [(hotel, oldest) for hotel, oldest, _ in self.hotels]

    def purge_expired(self, hotel_id: int, before: dt.date, *, limit: int) -> int:
        self.calls.append((hotel_id, before, limit))
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call:
            raise RuntimeError("boom")
        taken = min(limit, self.left[hotel_id])
        self.left[hotel_id] -= taken
        return taken


def hotel(hotel_id: int, zone: str = "UTC") -> Any:
    return SimpleNamespace(id=hotel_id, timezone=zone)


def run(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeRemoval,
    *,
    session: FakeSession | None = None,
    days: int = 730,
    batch_size: int = PURGE_BATCH,
    at: dt.datetime = NOON,
) -> PredictionPurgeResult:
    monkeypatch.setattr(retention_module, "MlPredictionRemovalRepository", lambda _session: fake)
    return purge_expired_predictions(
        session or FakeSession(),  # type: ignore[arg-type]
        settings(demand_prediction_retention_days=days),
        batch_size=batch_size,
        clock=lambda: at,
    )


def test_each_hotel_is_purged_up_to_its_own_cutoff(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRemoval([(hotel(1), dt.date(2020, 1, 1), 3), (hotel(2), dt.date(2021, 1, 1), 2)])

    result = run(monkeypatch, fake)

    assert fake.calls == [(1, dt.date(2024, 10, 9), PURGE_BATCH), (2, dt.date(2024, 10, 9), 1000)]
    assert result == PredictionPurgeResult(
        predictions_deleted=5, hotels=2, batches=2, retention_days=730
    )


def test_a_hotel_whose_oldest_prediction_is_the_cutoff_is_not_touched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cutoff day itself is kept, so no DELETE is issued and no batch counted."""
    fake = FakeRemoval([(hotel(1), dt.date(2024, 10, 9), 0), (hotel(2), dt.date(2024, 10, 8), 1)])

    result = run(monkeypatch, fake)

    assert [call[0] for call in fake.calls] == [2]
    assert (result.predictions_deleted, result.hotels, result.batches) == (1, 1, 1)


@pytest.mark.parametrize(
    ("instant", "zone", "cutoff"),
    [
        # 22:30 UTC is already the next day in Athens and still today in New York.
        (dt.datetime(2026, 10, 9, 22, 30, tzinfo=dt.UTC), "Europe/Athens", dt.date(2024, 10, 10)),
        (dt.datetime(2026, 10, 9, 22, 30, tzinfo=dt.UTC), "UTC", dt.date(2024, 10, 9)),
        (dt.datetime(2026, 10, 9, 2, 0, tzinfo=dt.UTC), "America/New_York", dt.date(2024, 10, 8)),
        # A zone the runtime does not know falls back to UTC, as every hotel "today" does.
        (dt.datetime(2026, 10, 9, 22, 30, tzinfo=dt.UTC), "Mars/Olympus", dt.date(2024, 10, 9)),
    ],
)
def test_the_cutoff_is_taken_in_the_hotels_own_calendar(
    monkeypatch: pytest.MonkeyPatch, instant: dt.datetime, zone: str, cutoff: dt.date
) -> None:
    fake = FakeRemoval([(hotel(1, zone), dt.date(2000, 1, 1), 1)])

    run(monkeypatch, fake, at=instant)

    assert fake.calls == [(1, cutoff, PURGE_BATCH)]


def test_the_clock_is_read_once_for_every_hotel(monkeypatch: pytest.MonkeyPatch) -> None:
    readings: list[dt.datetime] = []

    def clock() -> dt.datetime:
        readings.append(NOON)
        return NOON

    fake = FakeRemoval([(hotel(n), dt.date(2000, 1, 1), 1) for n in range(1, 4)])
    monkeypatch.setattr(retention_module, "MlPredictionRemovalRepository", lambda _session: fake)

    purge_expired_predictions(FakeSession(), settings(), clock=clock)  # type: ignore[arg-type]

    assert len(readings) == 1


def test_batches_run_until_one_is_short_and_each_is_committed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRemoval([(hotel(1), dt.date(2000, 1, 1), 5), (hotel(2), dt.date(2000, 1, 1), 4)])
    session = FakeSession()

    result = run(monkeypatch, fake, session=session, batch_size=2)

    assert [call[0] for call in fake.calls] == [1, 1, 1, 2, 2, 2]
    assert (result.predictions_deleted, result.hotels, result.batches) == (9, 2, 6)
    # One commit ends the read; then one per batch.
    assert (session.commits, session.rollbacks) == (1 + 6, 0)


def test_a_full_last_batch_is_followed_by_one_that_confirms_nothing_is_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRemoval([(hotel(1), dt.date(2000, 1, 1), 4)])

    result = run(monkeypatch, fake, batch_size=2)

    assert (result.predictions_deleted, result.batches) == (4, 3)


def test_a_failed_batch_is_rolled_back_and_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRemoval([(hotel(1), dt.date(2000, 1, 1), 5)], fail_on_call=2)
    session = FakeSession()

    with pytest.raises(RuntimeError, match="boom"):
        run(monkeypatch, fake, session=session, batch_size=2)

    assert (session.commits, session.rollbacks) == (1 + 1, 1)
    assert fake.left == {1: 3}


def test_a_failed_read_is_rolled_back_and_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken(FakeRemoval):
        def hotels_with_predictions(self) -> list[tuple[Any, dt.date]]:
            raise RuntimeError("read failed")

    session = FakeSession()
    with pytest.raises(RuntimeError, match="read failed"):
        run(monkeypatch, Broken([]), session=session)
    assert (session.commits, session.rollbacks) == (0, 1)


@pytest.mark.parametrize("size", [0, -1])
def test_a_batch_size_below_one_is_refused(monkeypatch: pytest.MonkeyPatch, size: int) -> None:
    with pytest.raises(ValueError, match="batch_size"):
        run(monkeypatch, FakeRemoval([]), batch_size=size)


def test_the_default_batch_is_a_thousand() -> None:
    assert PURGE_BATCH == 1_000


def test_an_empty_table_is_a_purge_of_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    assert run(monkeypatch, FakeRemoval([])) == PredictionPurgeResult(0, 0, 0, 730)


def test_the_result_is_counts_only() -> None:
    assert [field.name for field in dataclasses.fields(PredictionPurgeResult)] == [
        "predictions_deleted",
        "hotels",
        "batches",
        "retention_days",
    ]


def test_the_log_event_is_counts_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret = SimpleNamespace(id=4242, timezone="Europe/Athens")
    fake = FakeRemoval([(secret, dt.date(2001, 2, 3), 3)])

    with caplog.at_level(logging.INFO, logger="app.services.demand_prediction_retention"):
        run(monkeypatch, fake)

    [record] = caplog.records
    assert record.getMessage() == "demand prediction purge: 3 deleted at 1 hotel(s) in 1 batch(es)"
    assert record.__dict__["demand_predictions_purged"] == 3
    assert record.__dict__["purge_hotels"] == 1
    assert record.__dict__["purge_batches"] == 1
    rendered = repr(record.__dict__)
    for leak in ("4242", "Europe/Athens", "2001", "2024-10"):
        assert leak not in rendered, leak


def test_the_job_takes_no_actor() -> None:
    import inspect

    assert list(inspect.signature(purge_expired_predictions).parameters) == [
        "session",
        "settings",
        "batch_size",
        "clock",
    ]
    body = inspect.getsource(purge_expired_predictions).split('"""')[-1]
    for forbidden in ("actor", "current_user", "AuditTrail", "artifact_store", "generated_at"):
        assert forbidden not in body, forbidden


# ======================================================================================
# Who deletes a prediction, and who can reach the purge
# ======================================================================================


def test_only_the_removal_repository_deletes_a_prediction() -> None:
    deleting = sorted(
        name
        for name, source in sources().items()
        if "delete(DemandPrediction" in source or "DELETE FROM demand_predictions" in source
    )
    assert deleting == ["repositories/ml_prediction_removal.py"]


def test_the_removal_repository_has_exactly_three_methods_and_never_commits() -> None:
    tree = ast.parse((APP / "repositories" / "ml_prediction_removal.py").read_text("utf-8"))
    methods = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    }
    assert methods == {"hotels_with_predictions", "purge_expired", "delete_for_hotel"}
    calls = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    for forbidden in ("self._session.commit", "self._session.rollback", "insert", "update"):
        assert forbidden not in calls, forbidden


def test_the_purge_deletes_by_target_date_and_never_by_generation_time() -> None:
    tree = ast.parse((APP / "repositories" / "ml_prediction_removal.py").read_text("utf-8"))
    purge = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "purge_expired"
    )
    comparisons = [ast.unparse(node) for node in ast.walk(purge) if isinstance(node, ast.Compare)]
    assert "DemandPrediction.target_date < before" in comparisons
    assert not [text for text in comparisons if "generated_at" in text]


def test_only_the_retention_service_and_the_hotel_service_use_the_removal_repository() -> None:
    users = sorted(
        name
        for name, source in sources().items()
        if "MlPredictionRemovalRepository" in source
        and name != "repositories/ml_prediction_removal.py"
    )
    assert users == ["api/deps.py", "services/demand_prediction_retention.py", "services/hotel.py"]


def test_the_hotel_service_only_ever_removes_a_whole_hotels_predictions() -> None:
    source = sources()["services/hotel.py"]
    assert "self._predictions.delete_for_hotel(" in source
    assert "purge_expired" not in source
    assert "hotels_with_predictions" not in source


def test_no_route_or_request_dependency_reaches_the_purge() -> None:
    importers = sorted(
        name
        for name, source in sources().items()
        if "app.services.demand_prediction_retention" in source
        or "purge_expired_predictions" in source
    )
    assert importers == [
        "jobs/purge_demand_predictions.py",
        "services/demand_prediction_retention.py",
    ]


# ======================================================================================
# The hotel delete
# ======================================================================================


class Recorder:
    """Session, repositories and policy of a HotelService, writing one shared call log."""

    def __init__(self, fail: bool = False) -> None:
        self.log: list[str] = []
        self.fail = fail
        self.hotel = SimpleNamespace(id=7, public_id=uuid.uuid4())

    # session
    def commit(self) -> None:
        self.log.append("commit")

    def rollback(self) -> None:
        self.log.append("rollback")

    # hotel repository
    def get_by_public_id(self, public_id: uuid.UUID) -> Any:
        return self.hotel

    def delete(self, hotel: Any) -> None:
        self.log.append(f"hotel.delete({hotel.id})")
        if self.fail:
            orig = SimpleNamespace(sqlstate="23001", diag=SimpleNamespace(constraint_name="x"))
            raise IntegrityError("DELETE", {}, orig)  # type: ignore[arg-type]

    # memberships and predictions share the method name, so each gets its own facade
    def facade(self, label: str) -> Any:
        log = self.log
        return SimpleNamespace(delete_for_hotel=lambda hotel_id: log.append(f"{label}({hotel_id})"))

    # policy
    def require_role(self, hotel: Any, required: Any) -> None:
        return None


def service(recorder: Recorder) -> HotelService:
    return HotelService(
        recorder,  # type: ignore[arg-type]
        recorder,  # type: ignore[arg-type]
        recorder.facade("memberships"),
        recorder,  # type: ignore[arg-type]
        recorder.facade("predictions"),
    )


def test_a_hotels_predictions_go_in_the_delete_transaction_before_the_hotel() -> None:
    recorder = Recorder()

    service(recorder).delete(recorder.hotel.public_id)

    assert recorder.log == ["memberships(7)", "predictions(7)", "hotel.delete(7)", "commit"]


def test_a_refused_delete_rolls_the_whole_transaction_back_with_the_safe_message() -> None:
    recorder = Recorder(fail=True)

    with pytest.raises(ConflictError) as raised:
        service(recorder).delete(recorder.hotel.public_id)

    assert recorder.log == ["memberships(7)", "predictions(7)", "hotel.delete(7)", "rollback"]
    assert str(raised.value) == UNDELETABLE_HOTEL_MESSAGE


def test_the_message_no_longer_presents_a_list_of_record_kinds() -> None:
    message = UNDELETABLE_HOTEL_MESSAGE.lower()
    assert "cannot be deleted" in message
    assert "deactivate the hotel" in message
    for listed in ("room types", "rooms", "guests", "bookings", "revenue", "expenses", "remove"):
        assert listed not in message, listed
    for internal in ("fk_", "constraint", "restrict", "sql", "prediction"):
        assert internal not in message, internal


def test_the_dependency_gives_the_hotel_service_the_removal_repository() -> None:
    session: Any = object()
    built = deps.get_hotel_service(session, SimpleNamespace())  # type: ignore[arg-type]
    assert isinstance(built._predictions, MlPredictionRemovalRepository)
    assert built._predictions._session is session
