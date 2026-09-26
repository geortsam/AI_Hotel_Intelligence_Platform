"""Stage 6.11: the read contract, the ordering rule, the bounds, and the event.

Everything here runs without a database. What needs one -- rows actually returned, the ordering
as PostgreSQL applies it, pages that neither skip nor repeat, tenant isolation, and the proof
that a read writes nothing -- is in ``tests/integration/test_prediction_read_api.py`` against
real PostgreSQL.

The claim this file exists to pin hardest is the *disclosure* semantics: this endpoint returns
every stored row, where Stage 6.9's reader collapses them. Two rules, two methods, and a test
that says so.
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

from app.core.config import Settings
from app.core.errors import ValidationError
from app.main import create_app
from app.ml.serving import APPROVED_MODEL
from app.schemas.ml_prediction_read import StoredDemandPredictionResponse
from app.services.ml_prediction_read import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    DemandPredictionReadService,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPOSITORY_ROOT / "backend" / "app"
MIGRATION = (
    REPOSITORY_ROOT
    / "database"
    / "migrations"
    / "versions"
    / "20260921_0011_demand_prediction_public_id.py"
).read_text(encoding="utf-8")

SCHEMA_PATH = "/api/v1/hotels/{hotel_public_id}/ml/demand-predictions"

WINDOW = (dt.date(2026, 4, 1), dt.date(2026, 4, 30))

#: Exactly what a stored prediction discloses. Ten, and the list is the contract.
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

#: What must never reach a client. The first two are internal keys; the rest were withheld with
#: reasons recorded in the schema module.
FORBIDDEN_FIELDS = (
    "id",
    "hotel_id",
    "feature_values",
    "feature_digest",
    "canonical_model_digest",
    "request_id",
    "artifact_sha256",
)


# --- capturing what the service logs ---------------------------------------------------------


class RecordingHandler(logging.Handler):
    """Collects records instead of formatting them."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def events() -> Iterator[RecordingHandler]:
    """Everything ``app.services.ml_prediction_read`` logs, captured at the source.

    Not caplog: it listens on root, and ``configure_logging`` replaces root's handlers the first
    time any test starts an application, so a caplog assertion here would pass alone and capture
    nothing in a full run.
    """
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


FIELDS_NOT_ATTACHED_BY_THIS_STAGE = frozenset(
    logging.LogRecord("n", logging.INFO, "p", 1, "m", None, None).__dict__
) | {"message", "asctime", "request_id"}


def attached(record: logging.LogRecord) -> dict[str, object]:
    return {k: v for k, v in record.__dict__.items() if k not in FIELDS_NOT_ATTACHED_BY_THIS_STAGE}


def field(record: logging.LogRecord, name: str) -> Any:
    return record.__dict__[name]


# --- stub collaborators -----------------------------------------------------------------------


@dataclass
class FakeHotel:
    id: int
    public_id: uuid.UUID


class FakeScope:
    def __init__(self, hotel: FakeHotel) -> None:
        self._hotel = hotel
        self.calls = 0

    def require_hotel(self, hotel_public_id: uuid.UUID) -> FakeHotel:
        self.calls += 1
        return self._hotel


@dataclass
class FakeRow:
    """What the repository hands back: an ORM row, reduced to what the service reads."""

    public_id: uuid.UUID
    target_date: dt.date
    forecast_horizon_days: int
    prediction_cutoff: dt.datetime
    predicted_room_nights: float
    model_name: str
    model_version: str
    feature_version: str
    dataset_version: str
    generated_at: dt.datetime


def row(day: int, predicted: float = 100.0) -> FakeRow:
    target = dt.date(2026, 4, day)
    return FakeRow(
        public_id=uuid.uuid4(),
        target_date=target,
        forecast_horizon_days=7,
        prediction_cutoff=dt.datetime.combine(
            target - dt.timedelta(days=6), dt.time.min, tzinfo=dt.UTC
        ),
        predicted_room_nights=predicted,
        model_name="demand_baseline",
        model_version="demand_baseline_v1",
        feature_version="v1",
        dataset_version="v1",
        generated_at=dt.datetime(2026, 3, day, 12, tzinfo=dt.UTC),
    )


class FakePredictions:
    def __init__(self, rows: list[FakeRow], total: int | None = None) -> None:
        self._rows = rows
        self._total = len(rows) if total is None else total
        self.calls: list[tuple[int, dt.date, dt.date, int, int]] = []

    def stored_predictions_page(
        self, hotel_id: int, date_from: dt.date, date_to: dt.date, *, page: int, page_size: int
    ) -> tuple[list[FakeRow], int]:
        self.calls.append((hotel_id, date_from, date_to, page, page_size))
        return self._rows, self._total


def build_service(
    rows: list[FakeRow], *, total: int | None = None
) -> tuple[DemandPredictionReadService, FakePredictions, FakeScope]:
    hotel = FakeHotel(id=4242, public_id=uuid.uuid4())
    predictions = FakePredictions(rows, total)
    scope = FakeScope(hotel)
    service = DemandPredictionReadService(
        predictions,  # type: ignore[arg-type]
        scope,  # type: ignore[arg-type]
    )
    return service, predictions, scope


def read(
    service: DemandPredictionReadService,
    *,
    date_from: dt.date = WINDOW[0],
    date_to: dt.date = WINDOW[1],
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> Any:
    return service.list_predictions(
        uuid.uuid4(), date_from=date_from, date_to=date_to, page=page, page_size=page_size
    )


def openapi() -> dict[str, Any]:
    return create_app(Settings(environment="test", debug=True)).openapi()


# ======================================================================================
# The response contract
# ======================================================================================


def test_the_response_carries_exactly_ten_fields() -> None:
    """As a set, so an eleventh cannot arrive without someone choosing it."""
    assert set(StoredDemandPredictionResponse.model_fields) == EXPECTED_FIELDS
    assert len(EXPECTED_FIELDS) == 10


@pytest.mark.parametrize("forbidden", FORBIDDEN_FIELDS)
def test_the_withheld_fields_are_withheld(forbidden: str) -> None:
    """Two internal keys and four fields withheld with reasons recorded in the schema module."""
    assert forbidden not in StoredDemandPredictionResponse.model_fields


def test_the_published_schema_exposes_the_same_ten_fields() -> None:
    """Not just the model: what OpenAPI publishes, which is what a client will read."""
    properties = openapi()["components"]["schemas"]["StoredDemandPredictionResponse"]["properties"]

    assert set(properties) == EXPECTED_FIELDS
    for forbidden in FORBIDDEN_FIELDS:
        assert forbidden not in properties, forbidden


def test_the_service_maps_every_field_from_the_stored_row() -> None:
    stored = row(5, predicted=137.5)
    service, _, _ = build_service([stored])

    item = read(service).items[0]

    assert item.public_id == stored.public_id
    assert item.target_date == stored.target_date
    assert item.forecast_horizon_days == 7
    assert item.prediction_cutoff == stored.prediction_cutoff
    assert item.predicted_room_nights == pytest.approx(137.5)
    assert item.model_name == stored.model_name
    assert item.model_version == "demand_baseline_v1"
    assert item.feature_version == "v1"
    assert item.dataset_version == "v1"
    assert item.generated_at == stored.generated_at


def test_no_internal_identifier_appears_in_a_serialised_response() -> None:
    service, _, scope = build_service([row(5)])

    rendered = read(service).model_dump_json()

    assert "hotel_id" not in rendered
    assert str(scope._hotel.id) not in rendered


# ======================================================================================
# The API contract
# ======================================================================================


def test_this_route_is_present_and_is_a_read() -> None:
    """Stage 6.11 moved 51/83 -> 52/84; Stage 7.3 moved it again, to 54/86.

    This stage's own claim is the route below, not the total, so the total is asserted as the
    current one and the route is pinned by name.
    """
    schema = openapi()
    methods = {"get", "post", "put", "patch", "delete", "head", "options"}
    operations = sum(1 for path in schema["paths"].values() for verb in path if verb in methods)

    # Stage 7.7 added POST .../copilot/ask: 54 / 86 -> 55 / 87.
    # Stage 7.9 added the knowledge documents and their search: 55 / 87 -> 60 / 93.
    # Stage 7.11 added the copilot conversations: 60 / 93 -> 63 / 98.
    # Stage 7.12 added the attention list: 63 / 98 -> 64 / 99.
    assert len(schema["paths"]) == 64
    assert operations == 99
    assert SCHEMA_PATH in schema["paths"]
    assert set(schema["paths"][SCHEMA_PATH]) == {"get"}


def test_the_route_declares_its_failure_modes_and_no_model_failure() -> None:
    """No 503: a read path has no artifact to find missing."""
    responses = openapi()["paths"][SCHEMA_PATH]["get"]["responses"]

    assert set(responses) == {"200", "404", "422"}
    for code in ("404", "422"):
        ref = responses[code]["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("/ErrorResponse")


def test_the_request_accepts_a_window_and_a_page_and_nothing_else() -> None:
    parameters = openapi()["paths"][SCHEMA_PATH]["get"]["parameters"]
    by_name = {parameter["name"]: parameter for parameter in parameters}

    assert set(by_name) == {"hotel_public_id", "date_from", "date_to", "page", "page_size"}
    assert by_name["date_from"]["required"] is True
    assert by_name["date_to"]["required"] is True
    assert by_name["page"]["required"] is False
    assert by_name["page_size"]["required"] is False


def test_the_page_bounds_are_published() -> None:
    parameters = openapi()["paths"][SCHEMA_PATH]["get"]["parameters"]
    page_size = next(p for p in parameters if p["name"] == "page_size")["schema"]
    page = next(p for p in parameters if p["name"] == "page")["schema"]

    assert page_size["default"] == DEFAULT_PAGE_SIZE == 20
    assert page_size["maximum"] == MAX_PAGE_SIZE == 100
    assert page_size["minimum"] == 1
    assert page["minimum"] == 1


def test_the_response_is_the_existing_page_envelope() -> None:
    """The V1 convention, not a second pagination model invented for this stage."""
    ref = openapi()["paths"][SCHEMA_PATH]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]["$ref"]

    assert "Page_StoredDemandPredictionResponse_" in ref
    page = openapi()["components"]["schemas"]["Page_StoredDemandPredictionResponse_"]
    assert set(page["properties"]) == {"items", "total", "page", "page_size", "pages"}


# ======================================================================================
# Disclosure semantics
# ======================================================================================


def test_the_read_and_the_accuracy_read_are_separate_methods() -> None:
    """The heart of Stage 6.11: two disclosure rules, and they must not become one.

    ``scorable_predictions`` collapses a target date so accuracy cannot double-count it;
    ``stored_predictions_page`` collapses nothing so history is not hidden. A flag on one method
    would put one query one edit away from serving the wrong rule to the wrong caller.
    """
    tree = ast.parse((BACKEND / "repositories" / "ml_prediction.py").read_text(encoding="utf-8"))
    methods = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    }

    assert "scorable_predictions" in methods
    assert "stored_predictions_page" in methods

    read_method = methods["stored_predictions_page"]
    calls = {ast.unparse(node.func) for node in ast.walk(read_method) if isinstance(node, ast.Call)}
    assert not [call for call in calls if call.endswith(".distinct")], (
        "the read path must not collapse rows"
    )

    scoring = methods["scorable_predictions"]
    scoring_calls = {
        ast.unparse(node.func) for node in ast.walk(scoring) if isinstance(node, ast.Call)
    }
    assert [call for call in scoring_calls if call.endswith(".distinct")], (
        "the accuracy path still collapses, which is what makes the two different"
    )


def test_the_declared_order_is_total() -> None:
    """``public_id`` is unique, so no two rows tie on all three keys.

    Pagination correctness rests on that: a non-total order lets equal rows swap between pages,
    so a client walking them could see one twice and another never.
    """
    tree = ast.parse((BACKEND / "repositories" / "ml_prediction.py").read_text(encoding="utf-8"))
    method = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "stored_predictions_page"
    )
    order_by = next(
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Call) and ast.unparse(node.func).endswith(".order_by")
    )

    assert [ast.unparse(argument) for argument in order_by.args] == [
        "DemandPrediction.target_date",
        "DemandPrediction.generated_at",
        "DemandPrediction.public_id",
    ]


def test_the_page_is_bounded_by_limit_and_offset() -> None:
    source = (BACKEND / "repositories" / "ml_prediction.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    method = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "stored_predictions_page"
    )
    calls = {ast.unparse(node.func) for node in ast.walk(method) if isinstance(node, ast.Call)}

    assert [call for call in calls if call.endswith(".limit")]
    assert [call for call in calls if call.endswith(".offset")]


# ======================================================================================
# Bounds and validation
# ======================================================================================


def test_a_window_that_ends_before_it_starts_is_refused() -> None:
    """422, not an empty page: a typo must not be mistaken for a quiet period."""
    service, predictions, _ = build_service([])

    with pytest.raises(ValidationError):
        read(service, date_from=WINDOW[1], date_to=WINDOW[0])

    assert predictions.calls == [], "nothing was read for a malformed window"


def test_a_single_day_window_is_valid() -> None:
    service, predictions, _ = build_service([row(5)])

    read(service, date_from=WINDOW[0], date_to=WINDOW[0])

    assert predictions.calls[0][1] == predictions.calls[0][2] == WINDOW[0]


@pytest.mark.parametrize("page", [0, -1])
def test_a_page_below_one_is_refused(page: int) -> None:
    service, _, _ = build_service([])

    with pytest.raises(ValidationError):
        read(service, page=page)


@pytest.mark.parametrize("page_size", [0, -1, MAX_PAGE_SIZE + 1, 1000])
def test_a_page_size_outside_the_bounds_is_refused(page_size: int) -> None:
    """Bounded at the service too, because it is callable programmatically."""
    service, _, _ = build_service([])

    with pytest.raises(ValidationError):
        read(service, page_size=page_size)


@pytest.mark.parametrize("page_size", [1, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE])
def test_a_page_size_inside_the_bounds_is_accepted(page_size: int) -> None:
    service, _, _ = build_service([row(5)])

    assert read(service, page_size=page_size).page_size == page_size


def test_the_page_envelope_is_built_from_the_repository_total() -> None:
    service, _, _ = build_service([row(day) for day in range(1, 6)], total=42)

    page = read(service, page=2, page_size=5)

    assert page.total == 42
    assert page.page == 2
    assert page.page_size == 5
    assert page.pages == 9


def test_an_empty_window_is_an_empty_page_not_an_error() -> None:
    service, _, _ = build_service([], total=0)

    page = read(service)

    assert page.items == []
    assert page.total == 0
    assert page.pages == 0
    assert page.page == 1


# ======================================================================================
# Security
# ======================================================================================


def test_the_hotel_is_resolved_before_anything_is_read() -> None:
    source = (BACKEND / "services" / "ml_prediction_read.py").read_text(encoding="utf-8")
    body = source[source.index("def list_predictions(") :]

    assert body.index("require_hotel") < body.index("stored_predictions_page")

    service, predictions, scope = build_service([row(5)])
    read(service)
    assert scope.calls == 1
    assert predictions.calls[0][0] == 4242, "bounded by the resolver's hotel id"


def test_a_refused_hotel_reads_nothing() -> None:
    class Refusing:
        def require_hotel(self, hotel_public_id: uuid.UUID) -> object:
            raise LookupError("Hotel not found.")

    predictions = FakePredictions([])
    service = DemandPredictionReadService(
        predictions,  # type: ignore[arg-type]
        Refusing(),  # type: ignore[arg-type]
    )

    with pytest.raises(LookupError):
        read(service)

    assert predictions.calls == []


def test_no_method_accepts_more_than_one_hotel() -> None:
    tree = ast.parse((BACKEND / "services" / "ml_prediction_read.py").read_text(encoding="utf-8"))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "list_predictions"
    )
    arguments = [
        argument.arg
        for argument in [*function.args.args, *function.args.kwonlyargs]
        if argument.arg != "self"
    ]

    assert not [name for name in arguments if name.endswith("s") and "hotel" in name]
    assert "hotel_public_id" in arguments


# ======================================================================================
# Read-only
# ======================================================================================


def test_the_service_holds_no_session_and_cannot_write() -> None:
    tree = ast.parse((BACKEND / "services" / "ml_prediction_read.py").read_text(encoding="utf-8"))
    init = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    parameters = {argument.arg for argument in [*init.args.args, *init.args.kwonlyargs]}

    assert "session" not in parameters
    assert "db" not in parameters

    calls = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    for forbidden in ("commit", "rollback", "flush", "add", "insert", "delete", "update"):
        assert not [call for call in calls if call.endswith(f".{forbidden}")], forbidden


def test_the_service_builds_no_sqlalchemy_query() -> None:
    source = (BACKEND / "services" / "ml_prediction_read.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }

    assert not [module for module in imported if module.startswith("sqlalchemy")]
    calls = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    for forbidden in ("select", "and_", "or_", "func.count"):
        assert forbidden not in calls, forbidden


@pytest.mark.parametrize("forbidden", ["ml.", "fastapi", "starlette", "sklearn"])
def test_the_service_imports_neither_the_offline_package_nor_a_framework(forbidden: str) -> None:
    source = (BACKEND / "services" / "ml_prediction_read.py").read_text(encoding="utf-8")
    pattern = re.compile(rf"^\s*(?:import|from)\s+{re.escape(forbidden)}", re.MULTILINE)

    assert not pattern.search(source)


def test_the_read_path_never_loads_the_artifact() -> None:
    for module in ("services/ml_prediction_read.py", "schemas/ml_prediction_read.py"):
        tree = ast.parse((BACKEND / module).read_text(encoding="utf-8"))
        calls = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        for forbidden in ("approved_model", "predict_room_nights", "load_artifact"):
            assert forbidden not in calls, f"{module}: {forbidden}"


# ======================================================================================
# Determinism
# ======================================================================================


@pytest.mark.parametrize(
    "module",
    [
        "services/ml_prediction_read.py",
        "schemas/ml_prediction_read.py",
        "repositories/ml_prediction.py",
    ],
)
def test_no_ambient_clock_on_the_read_path(module: str) -> None:
    tree = ast.parse((BACKEND / module).read_text(encoding="utf-8"))
    calls = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}

    for forbidden in (
        "dt.date.today",
        "dt.datetime.now",
        "dt.datetime.utcnow",
        "datetime.now",
        "datetime.utcnow",
        "date.today",
        "func.now",
    ):
        assert forbidden not in calls, f"{module}: {forbidden}"


def test_two_identical_reads_are_byte_identical() -> None:
    rows = [row(day, predicted=100.0 + day) for day in (1, 5, 9)]
    service, _, _ = build_service(rows)

    assert read(service).model_dump_json() == read(service).model_dump_json()


# ======================================================================================
# The migration
# ======================================================================================


def test_the_migration_is_the_eleventh_and_follows_the_tenth() -> None:
    assert 'revision: str = "0011_demand_prediction_public_id"' in MIGRATION
    assert 'down_revision: str | None = "0010_demand_predictions"' in MIGRATION


def test_the_migration_creates_no_table_and_alters_one_column_family() -> None:
    """Strictly additive on one table. A CREATE TABLE would be outside the approved scope."""
    assert "CREATE TABLE" not in MIGRATION
    assert "DROP TABLE" not in MIGRATION
    assert MIGRATION.count("ADD COLUMN public_id") == 1
    assert "uq_demand_predictions_public_id" in MIGRATION
    assert "gen_random_uuid()" in MIGRATION
    assert "CREATE EXTENSION" not in MIGRATION


def test_the_migration_downgrade_removes_only_what_it_added() -> None:
    downgrade = MIGRATION[MIGRATION.index("def downgrade()") :]

    assert "DROP CONSTRAINT IF EXISTS uq_demand_predictions_public_id" in downgrade
    assert "DROP COLUMN IF EXISTS public_id" in downgrade
    assert "DROP TABLE" not in downgrade


def test_the_migration_chain_is_eleven_revisions() -> None:
    versions = REPOSITORY_ROOT / "database" / "migrations" / "versions"
    revisions = sorted(path.name for path in versions.glob("*.py"))

    # Stage 7.9 added 0014 (`hotel_documents`, `hotel_document_chunks`).
    # Stage 7.11 added 0015 (`copilot_conversations`, `copilot_messages`).
    assert len(revisions) == 15
    assert revisions[-1] == "20260927_0015_copilot_conversations.py"


# ======================================================================================
# The event
# ======================================================================================


def test_one_read_emits_exactly_one_event(events: RecordingHandler) -> None:
    service, _, _ = build_service([row(5)])

    read(service)

    assert len([record for record in events.records if hasattr(record, "outcome")]) == 1


def test_the_event_carries_exactly_the_four_approved_fields(events: RecordingHandler) -> None:
    service, _, _ = build_service([row(5), row(6)])

    read(service)

    event = next(record for record in events.records if hasattr(record, "outcome"))
    assert set(attached(event)) == {
        "outcome",
        "model_version",
        "predictions_returned",
        "duration_ms",
    }
    assert field(event, "outcome") == "returned"
    assert field(event, "model_version") == "demand_baseline_v1"
    assert field(event, "predictions_returned") == 2
    assert isinstance(field(event, "duration_ms"), float)


def test_an_empty_read_says_so(events: RecordingHandler) -> None:
    service, _, _ = build_service([])

    read(service)

    event = next(record for record in events.records if hasattr(record, "outcome"))
    assert field(event, "outcome") == "empty"
    assert field(event, "predictions_returned") == 0


def test_the_event_names_nothing_a_hotel_owns(events: RecordingHandler) -> None:
    """Captured records, not a source scan.

    ``duration_ms`` is excluded and the forbidden values are deliberately distinctive: it is a
    machine timing whose digits are arbitrary, so a short value collides with it sooner or later
    and fails for a reason that is not a leak. It is pinned by name in the four-field test above.
    """
    stored = row(5, predicted=8317.25)
    service, _, scope = build_service([stored])

    read(service)

    assert events.records, "nothing was captured -- the assertion below would be vacuous"
    rendered = "\n".join(
        f"{record.getMessage()} "
        f"{ {k: v for k, v in attached(record).items() if k != 'duration_ms'}!r}"
        for record in events.records
    )
    for forbidden in (
        "8317.25",
        str(stored.public_id),
        str(scope._hotel.public_id),
        APPROVED_MODEL.canonical_model_digest,
        "2026-04-05",
        "date_from",
        "SELECT",
        "password",
        "secret",
        "/app/",
    ):
        assert forbidden not in rendered, forbidden


# ======================================================================================
# Model invariants and the claims boundary
# ======================================================================================


def test_the_model_identity_is_untouched() -> None:
    assert APPROVED_MODEL.model_version == "demand_baseline_v1"
    assert (
        APPROVED_MODEL.canonical_model_digest
        == "436bf6b3cc5f1a2cc1a2e7fa5e971cedcd0405aa5b7f07a84967b293118a8f70"
    )


def test_the_backend_requirements_did_not_move() -> None:
    declared = [
        line.strip().lower()
        for line in (REPOSITORY_ROOT / "backend" / "requirements.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert [line for line in declared if line.startswith("scikit-learn")] == ["scikit-learn==1.9.1"]
    for library in ("pandas", "torch", "tensorflow", "xgboost", "lightgbm", "numpy", "scipy"):
        assert not [line for line in declared if line.startswith(library)], library


def test_production_accuracy_remains_unestablished() -> None:
    docs = REPOSITORY_ROOT / "docs"
    card = (docs / "ml-model-card.md").read_text(encoding="utf-8")
    serving = (docs / "ml-serving.md").read_text(encoding="utf-8")
    runtime = (docs / "ml-production-runtime.md").read_text(encoding="utf-8")
    read_api = (docs / "ml-prediction-read-api.md").read_text(encoding="utf-8")

    assert "| Production accuracy established | **No** |" in card
    assert "No production accuracy is established" in serving
    assert "Production accuracy is not established" in runtime
    assert "establishes no production accuracy" in read_api


def test_the_endpoint_claims_nothing_about_quality() -> None:
    """The published description must not read as an endorsement of the numbers it returns."""
    description = openapi()["paths"][SCHEMA_PATH]["get"]["description"].lower()

    assert "no established production accuracy" in description
    for forbidden in ("accurate", "reliable", "trustworthy", "validated", "proven"):
        assert forbidden not in description, forbidden


def test_no_response_field_is_named_for_a_verdict() -> None:
    for forbidden in ("drift", "anomaly", "threshold", "alert", "accuracy", "error", "score"):
        assert not [
            name for name in StoredDemandPredictionResponse.model_fields if forbidden in name
        ], forbidden
