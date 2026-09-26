"""Stage 6.6: the serving boundary, its refusals, and the architecture that keeps it narrow.

Everything here runs without a database. What needs one -- authorization, tenant isolation,
real feature acquisition -- is in ``tests/integration/test_ml_serving_api.py``, against a real
PostgreSQL, because those are the claims SQLite could not honestly check.

Four groups carry the weight:

* **the refusals** -- nine ways an artifact can fail to be the approved model, each producing a
  refusal rather than a prediction, and the tampered-payload case producing it *before* the
  bytes are deserialised;
* **no training at request time** -- ``fit``, ``fit_predict`` and ``partial_fit`` are patched to
  raise and inference is required to succeed anyway;
* **load once, mutate never** -- the artifact is read one time per process even under
  concurrent first requests, and what it computes is unchanged after repeated scoring;
* **the boundary itself** -- exactly one module in the application imports ``ml``, the router
  imports neither scikit-learn nor SQLAlchemy nor pickle, and the production requirements
  gained nothing.
"""

from __future__ import annotations

import ast
import datetime as dt
import re
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sklearn.ensemble import HistGradientBoostingRegressor

from app.core.config import Settings
from app.core.errors import InsufficientHistoryError, ModelUnavailableError
from app.main import create_app
from app.ml import artifact_store
from app.ml.artifact_store import ArtifactLocation, ServedModel
from app.ml.serving import (
    APPROVED_MODEL,
    EXPECTED_CLAIMS,
    ArtifactRejectedError,
    ArtifactUnavailableError,
    InsufficientFeatureHistoryError,
    ServingError,
    build_feature_values,
    feature_window,
)
from app.schemas.ml_serving import MAX_REQUESTED_HORIZON_DAYS, SERVED_HORIZON_DAYS
from tests.artifact_builder import build_artifact, rewrite

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
APP = REPOSITORY_ROOT / "backend" / "app"

SCHEMA_PATH = "/api/v1/hotels/{hotel_public_id}/ml/demand-forecast"
#: Stage 6.11 added this one. It reads stored rows and scores nothing.
STORED_PREDICTIONS_PATH = "/api/v1/hotels/{hotel_public_id}/ml/demand-predictions"
#: Stage 7.3 added these two. Both measure stored rows and score nothing.
ACCURACY_PATH = "/api/v1/hotels/{hotel_public_id}/ml/forecast-accuracy"
DISTRIBUTION_PATH = "/api/v1/hotels/{hotel_public_id}/ml/prediction-distribution"

#: A fixed anchor, so every expectation below is arithmetic rather than "whatever today is".
TARGET = dt.date(2026, 6, 1)


# --- fixtures ------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def artifact(tmp_path_factory: pytest.TempPathFactory) -> ArtifactLocation:
    """One real artifact for the whole module. Built, never read from the working tree."""
    return build_artifact(tmp_path_factory.mktemp("serving-artifact"))


@pytest.fixture(autouse=True)
def isolated_store() -> Iterator[None]:
    """The store is process-wide state, so every test starts and ends from a known one."""
    artifact_store.configure(None)
    yield
    artifact_store.configure(None)


@pytest.fixture
def served(artifact: ArtifactLocation) -> ServedModel:
    artifact_store.configure(artifact)
    return artifact_store.approved_model()


def history(*, days: int = 28, start: int = 7, value: int = 40) -> dict[dt.date, int]:
    """Realised demand for the *days* days ending at the cutoff, all present."""
    return {TARGET - dt.timedelta(days=offset): value for offset in range(start, start + days)}


# --- the feature contract --------------------------------------------------------------------


def test_the_served_model_is_the_stage_65_artifact() -> None:
    """Pinned. A change to any of these is a change to what is being served."""
    assert APPROVED_MODEL.model_version == "demand_baseline_v1"
    assert APPROVED_MODEL.feature_version == "v1"
    assert APPROVED_MODEL.dataset_version == "v1"
    assert APPROVED_MODEL.forecast_horizon_days == 7
    assert APPROVED_MODEL.feature_columns == (
        "day_of_week",
        "day_of_month",
        "month",
        "week_of_year",
        "day_of_year",
        "is_weekend",
        "demand_lag_7",
        "demand_lag_14",
        "demand_lag_28",
    )
    assert APPROVED_MODEL.lag_days == (7, 14, 28)
    assert (
        APPROVED_MODEL.canonical_model_digest
        == "436bf6b3cc5f1a2cc1a2e7fa5e971cedcd0405aa5b7f07a84967b293118a8f70"
    )


def test_the_six_excluded_features_are_excluded() -> None:
    """The Stage 6.1 contract has fifteen columns; this model consumes nine of them."""
    excluded = {
        "demand_lag_1",
        "demand_rolling_mean_7",
        "demand_rolling_mean_14",
        "demand_rolling_mean_28",
        "on_books_room_nights_at_cutoff",
        "rooms_existing_at_cutoff",
    }

    assert not excluded & set(APPROVED_MODEL.feature_columns)
    assert len(APPROVED_MODEL.feature_columns) + len(excluded) == 15


def test_the_acquisition_window_ends_at_the_cutoff_and_never_later() -> None:
    """The leakage guarantee, expressed as query bounds rather than as a later filter."""
    window = feature_window(TARGET)

    assert window.date_to == TARGET - dt.timedelta(days=7)
    assert window.date_from == TARGET - dt.timedelta(days=28)
    assert window.days == 22
    assert window.date_to < TARGET, "the target day is outside the window that is read"


def test_the_cutoff_instant_is_utc_and_ends_the_cutoff_day() -> None:
    window = feature_window(TARGET)

    assert window.cutoff.tzinfo is dt.UTC
    assert window.cutoff == dt.datetime(2026, 5, 26, tzinfo=dt.UTC)
    assert window.cutoff.date() == window.date_to + dt.timedelta(days=1)


def test_the_feature_vector_is_in_the_models_own_column_order() -> None:
    values = build_feature_values(history(), TARGET)

    assert tuple(values) == APPROVED_MODEL.feature_columns
    assert all(isinstance(value, float) for value in values.values())


def test_the_calendar_features_come_from_the_target_date() -> None:
    values = build_feature_values(history(), TARGET)

    assert values["day_of_week"] == float(TARGET.weekday())
    assert values["day_of_month"] == 1.0
    assert values["month"] == 6.0
    assert values["day_of_year"] == float(TARGET.timetuple().tm_yday)
    assert values["is_weekend"] == 0.0


@pytest.mark.parametrize("lag", [7, 14, 28])
def test_each_lag_reads_exactly_its_own_day(lag: int) -> None:
    demand = history()
    demand[TARGET - dt.timedelta(days=lag)] = 99

    values = build_feature_values(demand, TARGET)

    assert values[f"demand_lag_{lag}"] == 99.0


def test_demand_on_the_target_day_cannot_reach_the_features() -> None:
    """Belt and braces beside the window bounds: even handed the target day, nothing reads it."""
    demand = history()
    demand[TARGET] = 100_000

    values = build_feature_values(demand, TARGET)

    assert 100_000.0 not in set(values.values())


@pytest.mark.parametrize("missing", [7, 14, 28])
def test_a_missing_lag_is_refused_rather_than_filled(missing: int) -> None:
    demand = history()
    del demand[TARGET - dt.timedelta(days=missing)]

    with pytest.raises(InsufficientFeatureHistoryError):
        build_feature_values(demand, TARGET)


def test_an_absent_day_is_not_read_as_zero() -> None:
    """Zero is a real demand value here. A hotel that sold nothing is not a hotel with no
    record, and the training dataset dropped such rows rather than imputing them."""
    present = build_feature_values({**history(), TARGET - dt.timedelta(days=14): 0}, TARGET)
    assert present["demand_lag_14"] == 0.0

    absent = history()
    del absent[TARGET - dt.timedelta(days=14)]
    with pytest.raises(InsufficientFeatureHistoryError):
        build_feature_values(absent, TARGET)


def test_the_insufficient_history_message_names_no_feature_or_table() -> None:
    with pytest.raises(InsufficientFeatureHistoryError) as raised:
        build_feature_values({}, TARGET)
    text = str(raised.value).lower()

    for leak in ("demand_lag", "booking_room_nights", "select", "hotel_id"):
        assert leak not in text


# --- loading the artifact: what is accepted -------------------------------------------------------


def test_the_approved_artifact_loads_and_carries_its_identity(served: ServedModel) -> None:
    assert served.model_version == APPROVED_MODEL.model_version
    assert served.feature_columns == APPROVED_MODEL.feature_columns
    assert served.artifact.forecast_horizon_days == 7
    assert served.artifact.canonical_model_digest == APPROVED_MODEL.canonical_model_digest


def test_the_loaded_artifact_still_declares_no_serving_approval_of_its_own(
    served: ServedModel,
) -> None:
    """The approval lives in the server, not in the file.

    ``serving_enabled`` stays ``false`` in the artifact, which is what keeps the offline
    loader's refusal of a self-authorising artifact in force. What authorises serving is
    ``APPROVED_MODEL`` in ``app.ml.serving`` -- a constant in reviewed application code.
    """
    claims = served.artifact.metadata["claims"]
    assert isinstance(claims, dict)

    assert claims["serving_enabled"] is False
    assert claims["production_ready"] is False
    assert dict(EXPECTED_CLAIMS) == {key: claims[key] for key in EXPECTED_CLAIMS}


# --- loading the artifact: what is refused --------------------------------------------------------


def refuse(location: ArtifactLocation) -> ServingError:
    artifact_store.configure(location)
    with pytest.raises((ArtifactRejectedError, ArtifactUnavailableError)) as raised:
        artifact_store.approved_model()
    return raised.value


def test_a_missing_payload_is_unavailable_not_rejected(
    artifact: ArtifactLocation, tmp_path: Path
) -> None:
    error = refuse(ArtifactLocation(payload=tmp_path / "gone.pkl", metadata=artifact.metadata))

    assert isinstance(error, ArtifactUnavailableError)


def test_missing_metadata_is_unavailable(artifact: ArtifactLocation, tmp_path: Path) -> None:
    error = refuse(ArtifactLocation(payload=artifact.payload, metadata=tmp_path / "gone.json"))

    assert isinstance(error, ArtifactUnavailableError)


def test_a_tampered_payload_is_refused_before_it_is_deserialised(
    artifact: ArtifactLocation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole trust boundary. Unpickling is arbitrary code execution, so a digest compared
    afterwards would be a digest compared too late."""
    import ml.artifact as offline

    corrupted = tmp_path / "model.pkl"
    corrupted.write_bytes(artifact.payload.read_bytes() + b"\x00")

    def explode(payload: bytes) -> object:
        raise AssertionError("the payload was deserialised despite a digest mismatch")

    monkeypatch.setattr(offline, "deserialise_model", explode)
    error = refuse(ArtifactLocation(payload=corrupted, metadata=artifact.metadata))

    assert isinstance(error, ArtifactRejectedError)


def test_an_unreadable_payload_is_refused(artifact: ArtifactLocation, tmp_path: Path) -> None:
    replaced = tmp_path / "model.pkl"
    replaced.write_bytes(b"not a pickle at all")

    assert isinstance(refuse(ArtifactLocation(replaced, artifact.metadata)), ArtifactRejectedError)


@pytest.mark.parametrize(
    ("change", "what"),
    [
        ({"model__model_version": "demand_baseline_v2"}, "model version"),
        ({"model__model_name": "something_else"}, "model name"),
        ({"model__forecast_horizon_days": 14}, "horizon"),
        ({"dataset__feature_version": "v2"}, "feature version"),
        ({"dataset__dataset_version": "v2"}, "dataset version"),
        ({"dataset__dataset_sha256": "0" * 64}, "dataset checksum"),
        ({"artifact__sha256": "0" * 64}, "payload checksum"),
        ({"artifact__format": "joblib"}, "artifact format"),
        ({"artifact__canonical_model_digest": "0" * 64}, "canonical digest"),
        ({"schema_version": "artifact_v2"}, "schema version"),
        ({"claims__serving_enabled": True}, "an artifact that authorises itself"),
        ({"claims__production_ready": True}, "an artifact that claims production readiness"),
        ({"dataset__feature_columns": ["day_of_week"]}, "a short column list"),
        (
            {
                "dataset__feature_columns": [
                    "day_of_month",
                    "day_of_week",
                    "month",
                    "week_of_year",
                    "day_of_year",
                    "is_weekend",
                    "demand_lag_7",
                    "demand_lag_14",
                    "demand_lag_28",
                ]
            },
            "the right columns in the wrong order",
        ),
        ({"artifact__probe_predictions": ["0.000000"]}, "probe predictions"),
        ({"claims": None}, "a missing claims block"),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_an_artifact_that_is_not_the_approved_one_is_refused(
    artifact: ArtifactLocation, tmp_path: Path, change: dict[str, object], what: str
) -> None:
    error = refuse(rewrite(artifact, tmp_path, **change))

    assert isinstance(error, ArtifactRejectedError), what


def test_the_probe_check_compares_the_estimator_and_not_two_declarations(
    artifact: ArtifactLocation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one check that asks the unpickled object what it computes.

    Everything else compares a declaration against a declaration. This makes the estimator
    itself answer on a fixed synthetic grid, and requires the answer recorded at build time.
    Patching the probe to report something else must therefore be a refusal.
    """
    import ml.artifact as offline

    monkeypatch.setattr(offline, "probe_predictions", lambda estimator, rows=64: ("0.000000",))
    error = refuse(artifact)

    assert isinstance(error, ArtifactRejectedError)


def test_a_refusal_is_remembered_rather_than_retried(
    artifact: ArtifactLocation, tmp_path: Path
) -> None:
    """A missing artifact must not become a filesystem probe on every request."""
    missing = ArtifactLocation(payload=tmp_path / "gone.pkl", metadata=artifact.metadata)
    artifact_store.configure(missing)

    for _ in range(5):
        with pytest.raises(ArtifactUnavailableError):
            artifact_store.approved_model()

    assert artifact_store.load_count() == 0


def test_an_absent_ml_runtime_is_unavailable_rather_than_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shipped API image carries no ``ml/`` and no scikit-learn. That must serve a 503,
    not prevent the application from starting."""
    import builtins

    real_import = builtins.__import__

    def refuse_ml(name: str, *args: object, **kwargs: object) -> object:
        if name == "ml.artifact" or name.startswith("ml."):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    artifact_store.reset()
    monkeypatch.setattr(builtins, "__import__", refuse_ml)

    with pytest.raises(ArtifactUnavailableError):
        artifact_store.approved_model()


# --- loading once, and not mutating ---------------------------------------------------------------


def test_the_artifact_is_read_once_per_process(artifact: ArtifactLocation) -> None:
    artifact_store.configure(artifact)

    first = artifact_store.approved_model()
    for _ in range(20):
        assert artifact_store.approved_model() is first

    assert artifact_store.load_count() == 1


def test_concurrent_first_requests_produce_one_load(artifact: ArtifactLocation) -> None:
    """FastAPI runs a synchronous endpoint in a threadpool, so two requests really can arrive
    together on a cold process. The lock is what makes that one load rather than sixteen."""
    artifact_store.configure(artifact)

    with ThreadPoolExecutor(max_workers=16) as pool:
        models = list(pool.map(lambda _: artifact_store.approved_model(), range(16)))

    assert artifact_store.load_count() == 1
    assert len({id(model) for model in models}) == 1


def test_scoring_does_not_change_what_the_model_computes(served: ServedModel) -> None:
    """Immutability, measured rather than asserted: the probe grid answers the same before and
    after the model has been scored repeatedly."""
    from ml.artifact import probe_predictions

    before = probe_predictions(served.artifact.estimator)
    features = build_feature_values(history(), TARGET)
    for _ in range(25):
        artifact_store.predict_room_nights(
            served, hotel_public_id=uuid.uuid4(), target_date=TARGET, features=features
        )

    assert probe_predictions(served.artifact.estimator) == before


def test_inference_is_deterministic(served: ServedModel) -> None:
    features = build_feature_values(history(), TARGET)
    hotel = uuid.uuid4()

    values = {
        artifact_store.predict_room_nights(
            served, hotel_public_id=hotel, target_date=TARGET, features=features
        )
        for _ in range(10)
    }

    assert len(values) == 1


def test_concurrent_inference_agrees_with_serial_inference(served: ServedModel) -> None:
    features = build_feature_values(history(), TARGET)
    hotel = uuid.uuid4()
    expected = artifact_store.predict_room_nights(
        served, hotel_public_id=hotel, target_date=TARGET, features=features
    )

    def score(_: int) -> float:
        return artifact_store.predict_room_nights(
            served, hotel_public_id=hotel, target_date=TARGET, features=features
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = set(pool.map(score, range(32)))

    assert results == {expected}


def test_the_model_cannot_tell_small_hotels_apart(served: ServedModel) -> None:
    """A measured limitation of the artifact, pinned so it cannot be quietly forgotten.

    ``HistGradientBoostingRegressor`` bins its inputs from the training data, and this model was
    fitted on two hotels whose daily demand runs to the hundreds. Every flat history from one
    room night a night to forty therefore lands in the same bin and scores identically -- around
    166 room nights, for a hotel that sells five.

    This is not a defect in the serving path, which faithfully returns what the model computes.
    It is the reason ``docs/ml-serving.md`` §9 says the model is not calibrated for a property
    whose scale differs from the two it was fitted on, and it is why the tenant-isolation tests
    seed levels far enough apart to be distinguishable.
    """
    values = {
        artifact_store.predict_room_nights(
            served,
            hotel_public_id=uuid.uuid4(),
            target_date=TARGET,
            features=build_feature_values(history(value=level), TARGET),
        )
        for level in (1, 5, 10, 40)
    }

    assert len(values) == 1, "the artifact now distinguishes small hotels; update the docs"
    assert values.pop() > 100, "and it answers with a number drawn from the training scale"


def test_different_history_produces_a_different_prediction(served: ServedModel) -> None:
    """Otherwise every test above would pass against a constant."""
    quiet = artifact_store.predict_room_nights(
        served,
        hotel_public_id=uuid.uuid4(),
        target_date=TARGET,
        features=build_feature_values(history(value=10), TARGET),
    )
    busy = artifact_store.predict_room_nights(
        served,
        hotel_public_id=uuid.uuid4(),
        target_date=TARGET,
        features=build_feature_values(history(value=200), TARGET),
    )

    assert quiet != busy


# --- no training at request time ------------------------------------------------------------------


def test_inference_never_trains(served: ServedModel, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make training impossible, then require a prediction anyway."""

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("serving called a training method")

    for name in ("fit", "fit_predict", "partial_fit"):
        if hasattr(HistGradientBoostingRegressor, name):
            monkeypatch.setattr(HistGradientBoostingRegressor, name, explode, raising=False)

    value = artifact_store.predict_room_nights(
        served,
        hotel_public_id=uuid.uuid4(),
        target_date=TARGET,
        features=build_feature_values(history(), TARGET),
    )

    assert isinstance(value, float)


@pytest.mark.parametrize("method", ["fit(", "fit_predict(", "partial_fit(", "_train"])
def test_no_serving_source_calls_a_training_method(method: str) -> None:
    for name in ("ml/serving.py", "ml/artifact_store.py", "services/ml_serving.py"):
        source = (APP / name).read_text(encoding="utf-8")
        assert method not in source, f"{name}: {method}"


# --- the boundary, structurally -------------------------------------------------------------------


def application_sources() -> list[Path]:
    return sorted(p for p in APP.rglob("*.py") if "__pycache__" not in p.parts)


def test_only_the_named_application_modules_import_the_offline_package() -> None:
    """Stage 6.5 asserted that none did. Stage 6.6 introduced one. Stage 6.9 named a second.

    The boundary is worth something only while the importers are named. A bridge nobody
    declared -- a convenience in a service, a shortcut in a router -- is how "the API cannot
    reach the offline package" quietly becomes "the API reaches it from four places".

    So this is an allowlist rather than a count, and the test below it pins **which** offline
    module each one may reach.
    """
    pattern = re.compile(r"^\s*(?:from|import)\s+ml\b", re.MULTILINE)
    importers = [
        source.relative_to(APP).as_posix()
        for source in application_sources()
        if pattern.search(source.read_text(encoding="utf-8"))
    ]

    assert sorted(importers) == ["ml/accuracy.py", "ml/artifact_store.py"]


def test_each_bridge_reaches_only_its_own_offline_module() -> None:
    """Two bridges, two disjoint targets, and nothing else anywhere in the application.

    ``ml.metrics`` is pure standard library and is already one of the thirteen modules the
    production image ships, so the Stage 6.9 bridge adds no dependency and no image content --
    but it would still be wrong for a service, a schema or a repository to reach it directly,
    and this is what says so. A type-only import counts: it is still this file naming that
    module, and the value of "one bridge" is that grepping for it finds one place.
    """
    allowed = {
        "ml/artifact_store.py": {"ml.artifact", "ml.inference"},
        "ml/accuracy.py": {"ml.metrics"},
    }
    pattern = re.compile(r"^\s*(?:from|import)\s+(ml(?:\.[\w.]+)?)\b", re.MULTILINE)

    for source in application_sources():
        name = source.relative_to(APP).as_posix()
        reached = set(pattern.findall(source.read_text(encoding="utf-8")))
        assert reached <= allowed.get(name, set()), f"{name} reaches {reached}"


def test_the_offline_import_is_deferred_and_guarded() -> None:
    """Top-level, it would make ``ml`` a hard dependency of the whole application and the API
    would fail to start wherever it is absent -- which is every shipped image."""
    tree = ast.parse((APP / "ml" / "artifact_store.py").read_text(encoding="utf-8"))
    at_module_level = {
        alias.name.split(".")[0]
        for node in tree.body
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in getattr(node, "names", [])
    }
    module_origins = {
        node.module.split(".")[0]
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert "ml" not in at_module_level | module_origins


def test_the_serving_contract_module_reaches_no_ml_runtime_at_all() -> None:
    """``app.ml.serving`` is pure: the features and the approval, and nothing that imports."""
    source = (APP / "ml" / "serving.py").read_text(encoding="utf-8")

    for forbidden in ("sklearn", "numpy", "scipy", "pickle", "sqlalchemy", "fastapi", "import ml"):
        assert not re.search(rf"^\s*(?:import|from)\s+{re.escape(forbidden)}\b", source, re.M)


def test_the_serving_contract_module_knows_only_the_stage_61_features() -> None:
    """`app.ml.serving` may reach exactly one thing inside the application: the pure Stage 6.1
    feature contract. Anything else would put a service, a session or a request behind the
    module that decides what the model consumes."""
    source = (APP / "ml" / "serving.py").read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:from|import)\s+(app[\w.]*)", source, re.MULTILINE)

    assert set(imports) == {"app.ml.dataset"}


@pytest.mark.parametrize("forbidden", ["sqlalchemy", "fastapi", "starlette", "app.services"])
def test_the_artifact_store_holds_no_persistence_or_http_concern(forbidden: str) -> None:
    source = (APP / "ml" / "artifact_store.py").read_text(encoding="utf-8")

    assert not re.search(rf"^\s*(?:import|from)\s+{re.escape(forbidden)}\b", source, re.M)


@pytest.mark.parametrize(
    "library", ["sklearn", "numpy", "scipy", "pandas", "torch", "joblib", "pickle"]
)
def test_the_application_package_imports_no_ml_library_directly(library: str) -> None:
    """The approved dependency path is ``app.ml.artifact_store -> ml``. Not a second one."""
    pattern = re.compile(rf"^\s*(import|from)\s+{re.escape(library)}\b", re.MULTILINE)
    for source in application_sources():
        assert not pattern.search(source.read_text(encoding="utf-8")), f"{source}: {library}"


def test_the_backend_requirements_declare_only_the_serving_dependency() -> None:
    """Stage 6.6 left this at zero; Stage 6.7 added scikit-learn so the image can serve.

    The full packaging contract lives in tests/backend/test_production_packaging.py. What this
    keeps is the narrow claim the serving boundary depends on: one ML package, pinned, and no
    second one arriving alongside it.
    """
    declared = [
        line.strip().lower()
        for line in (REPOSITORY_ROOT / "backend" / "requirements.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert [line for line in declared if line.startswith("scikit-learn")] == ["scikit-learn==1.9.1"]
    for library in ("pandas", "torch", "tensorflow", "xgboost", "lightgbm"):
        assert not [line for line in declared if line.startswith(library)], library


@pytest.mark.parametrize(
    "forbidden", ["sklearn", "sqlalchemy", "pickle", "ml.artifact", "ml.inference", "app.models"]
)
def test_the_router_holds_no_ml_or_persistence_concern(forbidden: str) -> None:
    source = (APP / "api" / "v1" / "endpoints" / "ml_predictions.py").read_text(encoding="utf-8")
    pattern = re.compile(rf"^\s*(?:import|from)\s+{re.escape(forbidden)}\b", re.MULTILINE)

    assert not pattern.search(source)
    assert "load_artifact" not in source
    assert "open(" not in source


def test_the_serving_service_imports_no_http_framework_and_no_estimator() -> None:
    """Stage 6.6 forbade `sqlalchemy` outright here, because the service wrote nothing.

    Stage 6.8 gave it a transaction boundary, so it now imports the two names every writing
    service in this codebase imports -- `sqlalchemy.exc.IntegrityError` and
    `sqlalchemy.orm.Session`. That is the established pattern, not an exception carved for this
    service: `app/services/hotel.py` does exactly the same.

    What has not changed is the rule underneath: a service holds a session, it does not build
    queries with it. `test_services_build_no_sqlalchemy_queries` in the architecture audit
    enforces that across every service, and the check below pins the narrower claim for this
    one -- no HTTP framework, no estimator, and no route to the artifact except through the
    store.
    """
    source = (APP / "services" / "ml_serving.py").read_text(encoding="utf-8")

    for forbidden in ("fastapi", "starlette", "sklearn", "ml.artifact", "ml.inference"):
        assert not re.search(rf"^\s*(?:import|from)\s+{re.escape(forbidden)}\b", source, re.M)

    sqlalchemy_imports = set(re.findall(r"^from (sqlalchemy[\w.]*) import", source, re.M))
    assert sqlalchemy_imports == {"sqlalchemy.exc", "sqlalchemy.orm"}, sqlalchemy_imports


def test_nothing_reachable_from_a_request_points_the_store_at_a_path() -> None:
    """``configure`` exists for tests. A request must never choose an artifact location."""
    reachable = [
        path
        for path in application_sources()
        if path.parts[-2:] != ("ml", "artifact_store.py")
        and path.relative_to(APP).parts[0] in {"api", "services", "schemas", "repositories"}
    ]

    assert reachable, "the scan found nothing -- it would pass vacuously"
    for source in reachable:
        text = source.read_text(encoding="utf-8")
        assert "artifact_store.configure" not in text, source
        assert "ArtifactLocation(" not in text, source


def test_the_service_never_hands_the_store_a_path() -> None:
    tree = ast.parse((APP / "services" / "ml_serving.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "approved_model"
    ]

    assert calls, "the service does not reach the store at all -- the check is vacuous"
    for call in calls:
        assert not call.args and not call.keywords


# --- the HTTP contract ----------------------------------------------------------------------------


def openapi() -> dict:
    return create_app(Settings(environment="test", debug=True)).openapi()


def test_exactly_one_serving_route_was_added() -> None:
    """One route SCORES the model. The ML namespace holds three others that do not.

    The claim this test defends was never "the router has one route" -- it is that scoring the
    model happens in exactly one place. Stage 6.11 added ``GET .../ml/demand-predictions`` and
    Stage 7.3 added ``forecast-accuracy`` and ``prediction-distribution``; all three read rows
    the serving route already wrote and none touches an artifact. The test pins that separation
    rather than quietly widening to accommodate them.
    """
    schema = openapi()
    methods = {"get", "post", "put", "patch", "delete", "head", "options"}
    operations = sum(len([m for m in spec if m in methods]) for spec in schema["paths"].values())

    # Stage 7.7 added POST .../copilot/ask: 54 / 86 -> 55 / 87.
    # Stage 7.9 added the knowledge documents and their search: 55 / 87 -> 60 / 93.
    # Stage 7.11 added the copilot conversations: 60 / 93 -> 63 / 98.
    # Stage 7.12 added the attention list: 63 / 98 -> 64 / 99.
    assert len(schema["paths"]) == 64
    assert operations == 99
    assert sorted(path for path in schema["paths"] if "/ml/" in path) == sorted(
        [SCHEMA_PATH, STORED_PREDICTIONS_PATH, ACCURACY_PATH, DISTRIBUTION_PATH]
    )
    # Every route in the namespace is a read, and only one of them can reach an artifact --
    # which is why only one declares the 503 that a missing artifact produces.
    for path in (SCHEMA_PATH, STORED_PREDICTIONS_PATH, ACCURACY_PATH, DISTRIBUTION_PATH):
        assert set(schema["paths"][path]) == {"get"}
    assert "503" in schema["paths"][SCHEMA_PATH]["get"]["responses"]
    for path in (STORED_PREDICTIONS_PATH, ACCURACY_PATH, DISTRIBUTION_PATH):
        assert "503" not in schema["paths"][path]["get"]["responses"]


def test_the_stored_prediction_route_reaches_no_model() -> None:
    """Stage 6.11's route is a read path, and it cannot become a second serving path.

    It declares no 503: there is no artifact for it to find missing, because it loads none.
    That is the structural difference between the two routes, expressed in the contract rather
    than only in the prose.
    """
    schema = openapi()
    responses = set(schema["paths"][STORED_PREDICTIONS_PATH]["get"]["responses"])

    assert "503" not in responses, "a read path has no model to be unavailable"
    assert responses == {"200", "404", "422"}


def test_the_route_declares_its_failure_modes() -> None:
    responses = openapi()["paths"][SCHEMA_PATH]["get"]["responses"]

    assert set(responses) == {"200", "404", "422", "503"}
    for code in ("404", "422", "503"):
        ref = responses[code]["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("/ErrorResponse")


def test_the_request_accepts_a_hotel_a_date_and_a_horizon_and_nothing_else() -> None:
    operation = openapi()["paths"][SCHEMA_PATH]["get"]
    parameters = {p["name"]: p for p in operation["parameters"]}

    assert set(parameters) == {"hotel_public_id", "target_date", "horizon_days"}
    assert parameters["hotel_public_id"]["in"] == "path"
    assert parameters["hotel_public_id"]["schema"]["format"] == "uuid"
    assert parameters["target_date"]["required"] is True
    assert parameters["target_date"]["schema"]["format"] == "date"
    horizon = parameters["horizon_days"]["schema"]
    assert horizon["minimum"] == 1
    assert horizon["maximum"] == MAX_REQUESTED_HORIZON_DAYS
    assert horizon["default"] == SERVED_HORIZON_DAYS
    assert "requestBody" not in operation


@pytest.mark.parametrize(
    "forbidden",
    ["artifact", "artifact_path", "model_path", "estimator", "feature_columns", "features", "fit"],
)
def test_no_request_parameter_names_a_model_internal(forbidden: str) -> None:
    operation = openapi()["paths"][SCHEMA_PATH]["get"]

    assert forbidden not in {p["name"] for p in operation["parameters"]}


def test_the_response_schema_is_exactly_the_documented_contract() -> None:
    schemas = openapi()["components"]["schemas"]

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
    assert set(schemas["DemandModelMetadata"]["properties"]) == {
        "model_name",
        "model_version",
        "feature_version",
        "dataset_version",
        "status",
        "production_ready",
        "methodology",
    }


def test_the_response_carries_no_internal_identifier_and_no_wall_clock() -> None:
    schemas = openapi()["components"]["schemas"]
    fields = set(schemas["DemandPredictionResponse"]["properties"]) | set(
        schemas["DemandModelMetadata"]["properties"]
    )

    for banned in ("id", "hotel_id", "booking_id", "room_id", "generated_at", "created_at"):
        assert banned not in fields


# --- the domain errors the service raises ---------------------------------------------------------


def test_the_model_unavailable_error_is_a_503_that_describes_nothing() -> None:
    error = ModelUnavailableError()

    assert error.status_code == 503
    assert error.code == "MODEL_UNAVAILABLE"
    for leak in ("pickle", "sklearn", "sha256", "artifact.json", "\\", "/"):
        assert leak not in error.message


def test_the_insufficient_history_error_is_a_422_that_describes_nothing() -> None:
    error = InsufficientHistoryError()

    assert error.status_code == 422
    assert error.code == "INSUFFICIENT_HISTORY"
    for leak in ("demand_lag", "booking_room_nights", "hotel_id", "SELECT"):
        assert leak not in error.message
