"""Stage 6.8: the prediction record, its identity, and the serving-attempt event.

Everything here runs without a database. What needs one -- rows actually written, the unique
constraint actually enforcing, concurrency actually racing, refusals actually writing nothing --
is in ``tests/integration/test_prediction_persistence_api.py`` against real PostgreSQL, because
those are the claims SQLite or a mock could not honestly make.

The service is exercised here with stub collaborators. That is not a substitute for the
integration suite; it is how the OUTCOME MAPPING gets tested cheaply and exhaustively, one
branch per refusal, with the log records captured rather than the source read.
"""

from __future__ import annotations

import ast
import datetime as dt
import logging
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import UniqueConstraint

from app.core.config import Settings
from app.core.errors import InsufficientHistoryError, ModelUnavailableError, ValidationError
from app.db.base import Base
from app.main import create_app
from app.ml import artifact_store
from app.ml.artifact_store import ArtifactLocation
from app.ml.serving import (
    APPROVED_MODEL,
    FEATURE_DIGITS,
    ServingError,
    build_feature_values,
    feature_digest,
)
from app.models.prediction import (
    IDENTITY_COLUMNS,
    IDENTITY_CONSTRAINT,
)
from app.services.ml_serving import DemandPredictionService
from tests.artifact_builder import build_artifact

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (
    REPOSITORY_ROOT / "database" / "migrations" / "versions" / "20260920_0010_demand_predictions.py"
).read_text(encoding="utf-8")

#: The table as the metadata holds it. `DemandPrediction.__table__` is typed as a FromClause,
#: which carries no `constraints` or `indexes`; this is the same object, fully typed.
TABLE = Base.metadata.tables["demand_predictions"]

CANONICAL_DIGEST = "436bf6b3cc5f1a2cc1a2e7fa5e971cedcd0405aa5b7f07a84967b293118a8f70"
TARGET = dt.date(2026, 6, 1)


def history(level: int = 40) -> dict[dt.date, int]:
    return {TARGET - dt.timedelta(days=offset): level for offset in range(7, 29)}


# --- the feature digest ---------------------------------------------------------------------


def test_the_digest_is_reproducible_from_the_values_alone() -> None:
    """The property that lets a stored row be checked by hand rather than only by its writer."""
    values = build_feature_values(history(), TARGET)

    assert feature_digest(values) == feature_digest(dict(values))
    assert len(feature_digest(values)) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", feature_digest(values))


def test_the_digest_follows_the_documented_rule() -> None:
    """Recomputed here from the written-down rule, not by calling the function twice."""
    import hashlib
    import json

    values = build_feature_values(history(), TARGET)
    pairs = [[name, format(float(values[name]), f".{FEATURE_DIGITS}f")] for name in values]
    expected = hashlib.sha256(
        json.dumps(pairs, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()

    assert feature_digest(values) == expected


def test_a_changed_input_changes_the_digest() -> None:
    moved = history()
    moved[TARGET - dt.timedelta(days=7)] = 41

    assert feature_digest(build_feature_values(moved, TARGET)) != feature_digest(
        build_feature_values(history(), TARGET)
    )


def test_the_digest_is_order_sensitive() -> None:
    """The estimator consumes its columns positionally, so order is part of the inputs."""
    values = build_feature_values(history(), TARGET)
    reversed_order = dict(reversed(list(values.items())))

    with pytest.raises(ServingError):
        feature_digest(reversed_order)


def test_a_difference_below_the_recorded_precision_does_not_move_the_digest() -> None:
    """Six decimals: coarse enough that a last-bit difference cannot move it."""
    values = dict(build_feature_values(history(), TARGET))
    nudged = dict(values)
    nudged["demand_lag_7"] = values["demand_lag_7"] + 1e-12

    assert feature_digest(nudged) == feature_digest(values)


# --- the table ---------------------------------------------------------------------------------


def test_the_row_carries_exactly_the_defined_fields() -> None:
    columns = {column.name for column in TABLE.columns}

    assert columns == {
        "id",
        "hotel_id",
        "target_date",
        "forecast_horizon_days",
        "prediction_cutoff",
        "predicted_room_nights",
        "model_name",
        "model_version",
        "feature_version",
        "dataset_version",
        "canonical_model_digest",
        "feature_values",
        "feature_digest",
        "request_id",
        "generated_at",
    }


@pytest.mark.parametrize(
    "absent", ["public_id", "updated_at", "actor_user_id", "user_id", "artifact_sha256"]
)
def test_the_deliberately_absent_fields_are_absent(absent: str) -> None:
    """Each was considered and rejected in the design; this is what keeps them rejected."""
    assert absent not in {column.name for column in TABLE.columns}


def test_the_identity_is_the_five_columns_including_the_inputs() -> None:
    constraint = next(
        c
        for c in TABLE.constraints
        if isinstance(c, UniqueConstraint) and c.name == IDENTITY_CONSTRAINT
    )

    assert tuple(column.name for column in constraint.columns) == IDENTITY_COLUMNS
    assert "feature_digest" in IDENTITY_COLUMNS, "the inputs must be part of the identity"


def test_the_tenant_column_is_not_nullable_and_restricts_deletion() -> None:
    hotel_id = TABLE.columns["hotel_id"]
    foreign_key = next(iter(hotel_id.foreign_keys))

    assert hotel_id.nullable is False
    assert foreign_key.column.table.name == "hotels"
    assert foreign_key.ondelete == "RESTRICT"


def test_generated_at_is_defaulted_by_the_database() -> None:
    """So no caller can influence it, and a repeat cannot move it."""
    assert TABLE.columns["generated_at"].server_default is not None


def test_the_indexes_serve_the_two_documented_reads() -> None:
    indexes = {
        index.name: tuple(column.name for column in index.columns) for index in TABLE.indexes
    }

    assert indexes == {
        "ix_demand_predictions_hotel_id_target_date": ("hotel_id", "target_date"),
        "ix_demand_predictions_model_version_generated_at": ("model_version", "generated_at"),
    }


# --- the migration -------------------------------------------------------------------------------


def test_the_migration_is_the_tenth_and_follows_the_ninth() -> None:
    assert 'revision: str = "0010_demand_predictions"' in MIGRATION
    assert 'down_revision: str | None = "0009_audit_booking_deleted"' in MIGRATION


def test_the_migration_creates_one_table_and_alters_none() -> None:
    """Strictly additive. An ALTER on an existing table would be outside the approved scope."""
    assert MIGRATION.count("CREATE TABLE") == 1
    assert "demand_predictions" in MIGRATION
    for forbidden in ("ALTER TABLE", "DROP COLUMN", "ADD COLUMN"):
        assert forbidden not in MIGRATION, forbidden


def test_the_migration_drops_only_its_own_table() -> None:
    assert MIGRATION.count("DROP TABLE") == 1
    assert "DROP TABLE IF EXISTS demand_predictions" in MIGRATION


def test_the_migration_declares_the_identity_constraint_the_repository_names() -> None:
    """The constraint name is handed to ON CONFLICT, so the two must not drift."""
    assert IDENTITY_CONSTRAINT in MIGRATION
    repository = (
        REPOSITORY_ROOT / "backend" / "app" / "repositories" / "ml_prediction.py"
    ).read_text(encoding="utf-8")
    assert "IDENTITY_CONSTRAINT" in repository


# --- the repository writes one statement, and never commits ---------------------------------------


def test_the_insert_is_race_safe_by_construction() -> None:
    """Not SELECT-then-INSERT. The database evaluates the constraint as part of the write.

    The forbidden list is checked against CALLS rather than against the text, because this
    module's docstring explains at length that it neither commits nor rolls back -- and a
    substring search would read that explanation as the thing it rules out.
    """
    source = (REPOSITORY_ROOT / "backend" / "app" / "repositories" / "ml_prediction.py").read_text(
        encoding="utf-8"
    )
    calls = {
        ast.unparse(node.func) for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)
    }

    assert "on_conflict_do_nothing" in source
    assert "constraint=IDENTITY_CONSTRAINT" in source
    for forbidden in ("self._session.commit", "self._session.rollback", "select"):
        assert forbidden not in calls, forbidden


def test_the_service_owns_the_transaction_boundary() -> None:
    source = (REPOSITORY_ROOT / "backend" / "app" / "services" / "ml_serving.py").read_text(
        encoding="utf-8"
    )

    assert "self._session.commit()" in source
    assert "self._session.rollback()" in source


# --- the serving-attempt event, with stub collaborators -------------------------------------------


@dataclass
class FakeHotel:
    id: int
    public_id: uuid.UUID


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class FakeDemand:
    def __init__(self, level: int | None) -> None:
        self._level = level

    def demand_by_date(self, hotel_id: int, date_from: dt.date, date_to: dt.date) -> dict:
        if self._level is None:
            return {}
        return history(self._level)


class FakePredictions:
    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []

    def record(self, **row: Any) -> bool:
        self.recorded.append(row)
        return True


class FakeScope:
    def __init__(self, hotel: FakeHotel) -> None:
        self._hotel = hotel

    def require_hotel(self, hotel_public_id: uuid.UUID) -> FakeHotel:
        return self._hotel


@pytest.fixture(scope="module")
def artifact(tmp_path_factory: pytest.TempPathFactory) -> ArtifactLocation:
    return build_artifact(tmp_path_factory.mktemp("persistence-artifact"))


@pytest.fixture(autouse=True)
def isolated_store() -> Iterator[None]:
    artifact_store.configure(None)
    yield
    artifact_store.configure(None)


def build_service(
    *, level: int | None = 40, predictions: FakePredictions | None = None
) -> tuple[DemandPredictionService, FakeSession, FakePredictions]:
    session = FakeSession()
    recorder = predictions or FakePredictions()
    service = DemandPredictionService(
        session,  # type: ignore[arg-type]
        FakeDemand(level),  # type: ignore[arg-type]
        recorder,  # type: ignore[arg-type]
        FakeScope(FakeHotel(id=7, public_id=uuid.uuid4())),  # type: ignore[arg-type]
    )
    return service, session, recorder


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


def outcomes(records: list[logging.LogRecord]) -> list[str]:
    return [str(field(record, "outcome")) for record in records if hasattr(record, "outcome")]


def test_a_served_prediction_emits_one_event_and_records_one_row(
    artifact: ArtifactLocation, events: RecordingHandler
) -> None:
    artifact_store.configure(artifact)
    service, session, recorder = build_service()

    response = service.forecast_demand(uuid.uuid4(), TARGET, 7)

    assert outcomes(events.records) == ["served"]
    assert len(recorder.recorded) == 1
    assert session.commits == 1
    row = recorder.recorded[0]
    assert row["predicted_room_nights"] == response.predicted_room_nights
    assert row["canonical_model_digest"] == CANONICAL_DIGEST
    assert row["model_version"] == "demand_baseline_v1"
    assert row["feature_version"] == "v1"
    assert row["dataset_version"] == "v1"
    assert row["forecast_horizon_days"] == 7
    assert row["target_date"] == TARGET
    assert row["prediction_cutoff"] == response.prediction_cutoff
    assert set(row["feature_values"]) == set(APPROVED_MODEL.feature_columns)
    assert row["feature_digest"] == feature_digest(row["feature_values"])


def test_the_event_carries_the_four_approved_fields(
    artifact: ArtifactLocation, events: RecordingHandler
) -> None:
    artifact_store.configure(artifact)
    service, _, _ = build_service()

    service.forecast_demand(uuid.uuid4(), TARGET, 7)

    event = next(record for record in events.records if hasattr(record, "outcome"))
    assert field(event, "outcome") == "served"
    assert field(event, "model_version") == "demand_baseline_v1"
    assert field(event, "forecast_horizon_days") == 7
    assert isinstance(field(event, "duration_ms"), float)


@pytest.mark.parametrize(
    ("level", "horizon", "error", "expected"),
    [
        (40, 14, ValidationError, "invalid_request"),
        (None, 7, InsufficientHistoryError, "insufficient_history"),
    ],
)
def test_each_refusal_reports_its_own_outcome_and_records_nothing(
    artifact: ArtifactLocation,
    events: RecordingHandler,
    level: int | None,
    horizon: int,
    error: type[Exception],
    expected: str,
) -> None:
    artifact_store.configure(artifact)
    service, session, recorder = build_service(level=level)

    with pytest.raises(error):
        service.forecast_demand(uuid.uuid4(), TARGET, horizon)

    assert outcomes(events.records) == [expected]
    assert recorder.recorded == []
    assert session.commits == 0


def test_an_unavailable_model_reports_its_outcome_and_records_nothing(
    tmp_path: Path, artifact: ArtifactLocation, events: RecordingHandler
) -> None:
    artifact_store.configure(
        ArtifactLocation(payload=tmp_path / "gone.pkl", metadata=artifact.metadata)
    )
    service, session, recorder = build_service()

    with pytest.raises(ModelUnavailableError):
        service.forecast_demand(uuid.uuid4(), TARGET, 7)

    assert outcomes(events.records) == ["model_unavailable"]
    assert recorder.recorded == []
    assert session.commits == 0


def test_the_event_names_nothing_a_hotel_owns(
    artifact: ArtifactLocation, events: RecordingHandler
) -> None:
    """Captured records, not a source scan: the message AND the attached fields are checked."""
    artifact_store.configure(artifact)
    hotel = FakeHotel(id=424242, public_id=uuid.uuid4())
    session = FakeSession()
    service = DemandPredictionService(
        session,  # type: ignore[arg-type]
        FakeDemand(40),  # type: ignore[arg-type]
        FakePredictions(),  # type: ignore[arg-type]
        FakeScope(hotel),  # type: ignore[arg-type]
    )

    response = service.forecast_demand(uuid.uuid4(), TARGET, 7)

    assert events.records, "nothing was captured -- the assertion below would be vacuous"
    rendered = "\n".join(
        record.getMessage() + " " + repr(record.__dict__) for record in events.records
    )
    for forbidden in (
        str(response.predicted_room_nights),
        str(hotel.public_id),
        str(hotel.id),
        CANONICAL_DIGEST,
        "demand_lag_7",
        "/app/",
        "model.pkl",
    ):
        assert forbidden not in rendered, forbidden


def test_the_serving_path_logs_no_secret_or_sql(
    artifact: ArtifactLocation, events: RecordingHandler
) -> None:
    artifact_store.configure(artifact)
    service, _, _ = build_service()

    service.forecast_demand(uuid.uuid4(), TARGET, 7)

    assert events.records, "nothing was captured -- the assertion below would be vacuous"
    rendered = "\n".join(record.getMessage() for record in events.records).lower()
    for forbidden in ("select ", "insert ", "password", "secret", "constraint", "sqlstate"):
        assert forbidden not in rendered, forbidden


# --- the contract is unchanged ------------------------------------------------------


def test_the_api_surface_did_not_move() -> None:
    schema = create_app(Settings(environment="test", debug=True)).openapi()
    methods = {"get", "post", "put", "patch", "delete", "head", "options"}

    assert len(schema["paths"]) == 51
    assert sum(len([m for m in spec if m in methods]) for spec in schema["paths"].values()) == 83


def test_the_response_schema_gained_nothing() -> None:
    """Persistence is invisible from outside. No field, no parameter, no status code."""
    schemas = create_app(Settings(environment="test", debug=True)).openapi()["components"][
        "schemas"
    ]

    assert set(schemas["DemandPredictionResponse"]["properties"]) == {
        "hotel_public_id",
        "target_date",
        "forecast_horizon_days",
        "cutoff_date",
        "prediction_cutoff",
        "predicted_room_nights",
        "model",
        "features_used",
    }
    for banned in ("generated_at", "feature_digest", "prediction_id", "id"):
        assert banned not in schemas["DemandPredictionResponse"]["properties"], banned


def test_no_accuracy_claim_was_introduced() -> None:
    """Persisting predictions does not establish accuracy, and the documentation still says so.

    The three sentences below are the load-bearing ones. If a later stage ever does establish
    production accuracy, this test is what makes removing them a deliberate act.
    """
    docs = REPOSITORY_ROOT / "docs"
    card = (docs / "ml-model-card.md").read_text(encoding="utf-8")
    serving = (docs / "ml-serving.md").read_text(encoding="utf-8")
    runtime = (docs / "ml-production-runtime.md").read_text(encoding="utf-8")
    design = (docs / "ml-prediction-persistence-design.md").read_text(encoding="utf-8")

    assert "| Production accuracy established | **No** |" in card
    assert "No production accuracy is established" in serving
    assert "Production accuracy is not established" in runtime
    assert "Stage 6.8 makes no" in design and "accuracy claim" in design

    # And the artifact still says it in machine-readable form.
    assert APPROVED_MODEL.model_version == "demand_baseline_v1"
