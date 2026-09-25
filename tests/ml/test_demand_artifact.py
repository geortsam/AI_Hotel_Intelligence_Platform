"""Stage 6.5: the fitted artifact, its trust boundary and the offline inference contract.

The artifact is **built into a temporary directory** for these tests rather than read from the
working tree. That is deliberate: the payload is not committed, so a test that depended on one
being present would be testing the developer's disk. Building it here costs about a quarter of a
second and proves the thing that matters — that the artifact is reproducible from the committed
dataset and the pinned configuration.

Three tests carry the security weight:

* ``test_a_tampered_payload_is_refused_before_it_is_deserialised`` — the digest is checked first,
  which is the whole of the trust boundary.
* ``test_inference_never_trains`` — ``fit`` is monkey-patched to explode and inference is
  required to succeed anyway.
* ``test_the_application_package_imports_nothing_from_ml`` — the artifact cannot reach the API,
  structurally rather than by convention.
"""

from __future__ import annotations

import ast
import datetime as dt
import hashlib
import json
import math
import pickle
import re
import uuid
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest
from sklearn.ensemble import HistGradientBoostingRegressor

from ml.artifact import (
    ARTIFACT_FORMAT,
    ARTIFACT_SCHEMA_VERSION,
    DEFAULT_ARTIFACT_METADATA,
    HELD_OUT_PARTITIONS,
    PICKLE_PROTOCOL,
    PROBE_DIGITS,
    PROBE_ROWS,
    TRAINING_PARTITION,
    TRAINING_ROW_ORDER,
    ArtifactError,
    TrainedArtifact,
    assert_all_finite,
    assert_no_held_out_row,
    build_artifact_metadata,
    canonical_model_digest,
    load_artifact,
    load_artifact_metadata,
    probe_matrix,
    probe_predictions,
    serialise_model,
    train_artifact,
    training_rows,
)
from ml.inference import (
    DemandPrediction,
    FeatureVector,
    InferenceError,
    predict_demand,
)
from ml.loading import DEFAULT_DATASET, ProcessedDataset, ProcessedRow, load_processed_dataset
from ml.models import MODEL_NAME, MODEL_VERSION, EstimatorConfig, LearnedModel, design_matrix
from ml.pipelines.build_demand_artifact import registry_artifact_block
from ml.validation import CANONICAL_FEATURE_ORDER, ValidationError
from tests.ml.test_demand_model_validation import STAGE_63_ESTIMATOR_SHA256

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

#: The Stage 6.3 feature set at a seven-day horizon. Pinned, because the artifact consumes it
#: positionally and a change here is a change to what the model means.
EXPECTED_FEATURE_COLUMNS: tuple[str, ...] = (
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

#: The reproducibility digest of `demand_baseline_v1`, pinned. It is a function of the feature
#: columns, the estimator configuration, the training extent and the model's probe predictions,
#: so any real change to the model moves it.
CANONICAL_DIGEST = "436bf6b3cc5f1a2cc1a2e7fa5e971cedcd0405aa5b7f07a84967b293118a8f70"

#: The one route Stage 6.6 added. Named here so the two guards below cannot drift apart.
SERVING_ENDPOINT = "/api/v1/hotels/{hotel_public_id}/ml/demand-forecast"

#: The one Stage 6.11 added. On the same router, and deliberately not a serving route: it reads
#: stored rows, loads no artifact, and declares no 503.
STORED_PREDICTIONS_ENDPOINT = "/api/v1/hotels/{hotel_public_id}/ml/demand-predictions"

#: The two Stage 7.3 added, for the same reason and with the same property: they measure rows the
#: serving route already wrote, load no artifact, and declare no 503.
ACCURACY_ENDPOINT = "/api/v1/hotels/{hotel_public_id}/ml/forecast-accuracy"
DISTRIBUTION_ENDPOINT = "/api/v1/hotels/{hotel_public_id}/ml/prediction-distribution"

#: Every route under the ML prefix. Exactly one of them reaches the model.
ML_ENDPOINTS = [
    SERVING_ENDPOINT,
    STORED_PREDICTIONS_ENDPOINT,
    ACCURACY_ENDPOINT,
    DISTRIBUTION_ENDPOINT,
]


# --- fixtures ------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def dataset() -> ProcessedDataset:
    return load_processed_dataset(DEFAULT_DATASET)


@pytest.fixture(scope="module")
def trained(dataset: ProcessedDataset) -> TrainedArtifact:
    return train_artifact(dataset, dataset_version="v1", feature_version="v1")


@pytest.fixture(scope="module")
def artifact_paths(
    trained: TrainedArtifact, tmp_path_factory: pytest.TempPathFactory
) -> tuple[Path, Path]:
    """Write a real artifact pair into a temporary directory."""
    directory = tmp_path_factory.mktemp("artifact")
    payload_path = directory / "model.pkl"
    metadata_path = directory / "artifact.json"
    payload = serialise_model(trained.estimator)
    payload_path.write_bytes(payload)
    metadata = build_artifact_metadata(
        trained,
        artifact_sha256=hashlib.sha256(payload).hexdigest(),
        artifact_bytes=len(payload),
        artifact_filename=payload_path.name,
        dataset_path=DEFAULT_DATASET.name,
        validation_sha256="v" * 64,
        registry_sha256="r" * 64,
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    metadata_path.write_bytes(json.dumps(metadata, indent=2, sort_keys=True).encode("utf-8"))
    return payload_path, metadata_path


@pytest.fixture(scope="module")
def loaded(artifact_paths: tuple[Path, Path]) -> Any:
    return load_artifact(*artifact_paths)


def rewrite_metadata(paths: tuple[Path, Path], tmp_path: Path, **changes: Any) -> tuple[Path, Path]:
    """A copy of the artifact pair with the metadata edited, for the refusal tests."""
    payload_path, metadata_path = paths
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    for dotted, value in changes.items():
        block, _, key = dotted.partition("__")
        if key:
            metadata[block][key] = value
        else:
            metadata[block] = value
    new_metadata = tmp_path / "artifact.json"
    new_metadata.write_bytes(json.dumps(metadata, indent=2, sort_keys=True).encode("utf-8"))
    return payload_path, new_metadata


def vector(row: ProcessedRow, columns: tuple[str, ...], horizon: int = 7) -> FeatureVector:
    return FeatureVector(
        hotel_public_id=row.hotel_public_id,
        target_date=row.target_date,
        forecast_horizon_days=horizon,
        features={name: float(row.features[name]) for name in columns},  # type: ignore[arg-type]
    )


def scorable(
    dataset: ProcessedDataset, columns: tuple[str, ...], limit: int = 20
) -> list[FeatureVector]:
    rows = [
        row
        for row in dataset.rows
        if row.partition == "test" and all(row.features[name] is not None for name in columns)
    ]
    return [vector(row, columns) for row in rows[:limit]]


# --- 1./2. the artifact and its metadata exist ----------------------------------------------------


def test_the_built_artifact_and_its_metadata_exist(artifact_paths: tuple[Path, Path]) -> None:
    payload_path, metadata_path = artifact_paths
    assert payload_path.is_file()
    assert metadata_path.is_file()
    assert payload_path.stat().st_size > 0


def test_the_committed_metadata_exists_and_declares_the_expected_schema() -> None:
    assert DEFAULT_ARTIFACT_METADATA.is_file(), DEFAULT_ARTIFACT_METADATA
    metadata = load_artifact_metadata()
    assert metadata["schema_version"] == ARTIFACT_SCHEMA_VERSION
    artifact = metadata["artifact"]
    assert isinstance(artifact, dict)
    assert artifact["format"] == ARTIFACT_FORMAT
    assert artifact["committed"] is False


def test_the_payload_is_not_committed_and_the_metadata_says_so() -> None:
    """`.gitignore` has said since Stage 1 that weights are never committed. Still true."""
    tracked = (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "!ml/models/*/artifact.json" in tracked
    assert "!ml/models/*/model.pkl" not in tracked
    artifact = load_artifact_metadata()["artifact"]
    assert isinstance(artifact, dict)
    assert "arbitrary code on load" in str(artifact["committed_note"])


# --- 3. the artifact checksum is the trust boundary ----------------------------------------------


def test_a_matching_payload_and_metadata_load(loaded: Any) -> None:
    assert loaded.model_version == MODEL_VERSION
    assert loaded.feature_columns == EXPECTED_FEATURE_COLUMNS
    assert loaded.forecast_horizon_days == 7
    assert hasattr(loaded.estimator, "predict")


def test_a_tampered_payload_is_refused_before_it_is_deserialised(
    artifact_paths: tuple[Path, Path], tmp_path: Path
) -> None:
    """The digest is checked first. A payload that fails it is never handed to pickle."""
    payload_path, metadata_path = artifact_paths
    tampered = tmp_path / "model.pkl"
    payload = bytearray(payload_path.read_bytes())
    payload[-1] ^= 0xFF
    tampered.write_bytes(bytes(payload))
    with pytest.raises(ArtifactError, match="was not deserialised"):
        load_artifact(tampered, metadata_path)


def test_a_truncated_payload_is_refused(artifact_paths: tuple[Path, Path], tmp_path: Path) -> None:
    payload_path, metadata_path = artifact_paths
    truncated = tmp_path / "model.pkl"
    truncated.write_bytes(payload_path.read_bytes()[:1024])
    with pytest.raises(ArtifactError, match="hashes to"):
        load_artifact(truncated, metadata_path)


def test_a_replaced_payload_with_a_stale_digest_is_refused(
    artifact_paths: tuple[Path, Path], tmp_path: Path
) -> None:
    """A different model under the same metadata is the attack this check exists for."""
    _, metadata_path = artifact_paths
    impostor = tmp_path / "model.pkl"
    impostor.write_bytes(pickle.dumps({"not": "a model"}, protocol=PICKLE_PROTOCOL))
    with pytest.raises(ArtifactError, match="was not deserialised"):
        load_artifact(impostor, metadata_path)


def test_a_missing_payload_is_named_rather_than_guessed(
    artifact_paths: tuple[Path, Path], tmp_path: Path
) -> None:
    _, metadata_path = artifact_paths
    with pytest.raises(ArtifactError, match="generated rather than committed"):
        load_artifact(tmp_path / "absent.pkl", metadata_path)


def test_missing_metadata_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ArtifactError, match="does not exist"):
        load_artifact_metadata(tmp_path / "artifact.json")


# --- 4./5./6./7. identity validation -------------------------------------------------------------


def test_the_metadata_points_at_the_exact_dataset_checksum(
    loaded: Any, dataset: ProcessedDataset, artifact_paths: tuple[Path, Path]
) -> None:
    assert loaded.dataset_sha256 == dataset.sha256
    # ...and the load accepts it when the caller states the same expectation.
    load_artifact(*artifact_paths, expected_dataset_sha256=dataset.sha256)


def test_a_dataset_checksum_mismatch_is_refused(artifact_paths: tuple[Path, Path]) -> None:
    with pytest.raises(ArtifactError, match="was built from dataset"):
        load_artifact(*artifact_paths, expected_dataset_sha256="0" * 64)


def test_a_feature_version_mismatch_is_refused(artifact_paths: tuple[Path, Path]) -> None:
    with pytest.raises(ArtifactError, match="feature_version"):
        load_artifact(*artifact_paths, expected_feature_version="v2")


def test_a_model_version_mismatch_is_refused(artifact_paths: tuple[Path, Path]) -> None:
    with pytest.raises(ArtifactError, match="model_version"):
        load_artifact(*artifact_paths, expected_model_version="demand_baseline_v9")


def test_a_wrong_schema_version_is_refused(
    artifact_paths: tuple[Path, Path], tmp_path: Path
) -> None:
    paths = rewrite_metadata(artifact_paths, tmp_path, schema_version="artifact_v99")
    with pytest.raises(ArtifactError, match="artifact schema"):
        load_artifact(*paths)


def test_a_missing_metadata_block_is_named(
    artifact_paths: tuple[Path, Path], tmp_path: Path
) -> None:
    payload_path, metadata_path = artifact_paths
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    del metadata["dataset"]
    broken = tmp_path / "artifact.json"
    broken.write_bytes(json.dumps(metadata, indent=2, sort_keys=True).encode("utf-8"))
    with pytest.raises(ArtifactError, match="'dataset' block"):
        load_artifact(payload_path, broken)


def test_an_unsupported_format_is_refused(
    artifact_paths: tuple[Path, Path], tmp_path: Path
) -> None:
    metadata = json.loads(artifact_paths[1].read_text(encoding="utf-8"))
    metadata["artifact"]["format"] = "onnx"
    path = tmp_path / "artifact.json"
    path.write_bytes(json.dumps(metadata, indent=2, sort_keys=True).encode("utf-8"))
    with pytest.raises(ArtifactError, match="format"):
        load_artifact(artifact_paths[0], path)


def test_an_artifact_claiming_to_be_servable_is_refused(
    artifact_paths: tuple[Path, Path], tmp_path: Path
) -> None:
    """This stage does not serve anything, so an artifact that says it does is not ours."""
    metadata = json.loads(artifact_paths[1].read_text(encoding="utf-8"))
    metadata["claims"]["serving_enabled"] = True
    path = tmp_path / "artifact.json"
    path.write_bytes(json.dumps(metadata, indent=2, sort_keys=True).encode("utf-8"))
    with pytest.raises(ArtifactError, match="serving_enabled"):
        load_artifact(artifact_paths[0], path)


# --- 8./9./10./11. the feature contract ----------------------------------------------------------


def test_the_feature_columns_are_exactly_the_nine_admissible_at_seven_days(
    trained: TrainedArtifact,
) -> None:
    assert trained.feature_columns == EXPECTED_FEATURE_COLUMNS
    assert all(name in CANONICAL_FEATURE_ORDER for name in trained.feature_columns)
    assert "rooms_existing_at_cutoff" not in trained.feature_columns
    assert "demand_lag_1" not in trained.feature_columns


def test_a_missing_feature_is_refused_and_named(loaded: Any, dataset: ProcessedDataset) -> None:
    rows = scorable(dataset, loaded.feature_columns, limit=1)
    features = dict(rows[0].features)
    del features["demand_lag_14"]
    broken = FeatureVector(rows[0].hotel_public_id, rows[0].target_date, 7, features)
    with pytest.raises(InferenceError, match="demand_lag_14"):
        predict_demand(loaded, [broken])


def test_an_unexpected_feature_is_refused_and_named(loaded: Any, dataset: ProcessedDataset) -> None:
    rows = scorable(dataset, loaded.feature_columns, limit=1)
    features = {**rows[0].features, "competitor_price": 1.0}
    broken = FeatureVector(rows[0].hotel_public_id, rows[0].target_date, 7, features)
    with pytest.raises(InferenceError, match="competitor_price"):
        predict_demand(loaded, [broken])


def test_reordered_features_are_refused_rather_than_sorted(
    loaded: Any, dataset: ProcessedDataset
) -> None:
    """They are consumed positionally, so a permutation would score the wrong numbers."""
    rows = scorable(dataset, loaded.feature_columns, limit=1)
    original = rows[0].features
    names = list(original)
    names[0], names[1] = names[1], names[0]
    permuted = FeatureVector(
        rows[0].hotel_public_id,
        rows[0].target_date,
        7,
        {name: original[name] for name in names},
    )
    with pytest.raises(InferenceError, match="different order"):
        predict_demand(loaded, [permuted])


# --- 12./13./14. value and horizon validation ----------------------------------------------------


@pytest.mark.parametrize(
    ("value", "fragment"),
    [
        (float("nan"), "NaN"),
        (float("inf"), "infinite"),
        (float("-inf"), "infinite"),
        (True, "bool"),
        ("120", "str"),
        (None, "NoneType"),
    ],
)
def test_an_unusable_feature_value_is_refused(
    loaded: Any, dataset: ProcessedDataset, value: object, fragment: str
) -> None:
    rows = scorable(dataset, loaded.feature_columns, limit=1)
    features = {**rows[0].features, "demand_lag_7": value}
    broken = FeatureVector(rows[0].hotel_public_id, rows[0].target_date, 7, features)  # type: ignore[arg-type]
    with pytest.raises(InferenceError, match=fragment):
        predict_demand(loaded, [broken])


def test_a_wrong_horizon_is_refused(loaded: Any, dataset: ProcessedDataset) -> None:
    rows = scorable(dataset, loaded.feature_columns, limit=1)
    wrong = FeatureVector(rows[0].hotel_public_id, rows[0].target_date, 1, rows[0].features)
    with pytest.raises(InferenceError, match="horizon 1 does not match"):
        predict_demand(loaded, [wrong])


def test_a_non_uuid_identifier_and_a_datetime_target_are_refused(
    loaded: Any, dataset: ProcessedDataset
) -> None:
    rows = scorable(dataset, loaded.feature_columns, limit=1)
    with pytest.raises(InferenceError, match="must be a UUID"):
        predict_demand(
            loaded,
            [FeatureVector("not-a-uuid", rows[0].target_date, 7, rows[0].features)],  # type: ignore[arg-type]
        )
    with pytest.raises(InferenceError, match="must be a date"):
        predict_demand(
            loaded,
            [
                FeatureVector(
                    rows[0].hotel_public_id,
                    dt.datetime(2017, 5, 1, tzinfo=dt.UTC),
                    7,
                    rows[0].features,
                )
            ],
        )


def test_an_empty_request_returns_nothing_rather_than_failing(loaded: Any) -> None:
    assert predict_demand(loaded, []) == ()


# --- 15./16./17. determinism, reload, equivalence ------------------------------------------------


def test_predictions_are_deterministic(loaded: Any, dataset: ProcessedDataset) -> None:
    rows = scorable(dataset, loaded.feature_columns)
    assert predict_demand(loaded, rows) == predict_demand(loaded, rows)


def test_the_artifact_reloads_to_the_same_model(
    artifact_paths: tuple[Path, Path], dataset: ProcessedDataset
) -> None:
    first = load_artifact(*artifact_paths)
    second = load_artifact(*artifact_paths)
    assert probe_predictions(first.estimator) == probe_predictions(second.estimator)
    rows = scorable(dataset, first.feature_columns)
    assert predict_demand(first, rows) == predict_demand(second, rows)


def test_predictions_survive_a_round_trip_through_the_payload(
    trained: TrainedArtifact, artifact_paths: tuple[Path, Path], dataset: ProcessedDataset
) -> None:
    """The in-memory model and the reloaded one must agree exactly, not approximately."""
    loaded = load_artifact(*artifact_paths)
    assert probe_predictions(loaded.estimator) == probe_predictions(trained.estimator)


# --- 18./19./21. isolation -----------------------------------------------------------------------


OFFLINE_MODULES = ("ml/artifact.py", "ml/inference.py")

FORBIDDEN_IMPORTS = (
    "sqlalchemy",
    "psycopg",
    "alembic",
    "fastapi",
    "starlette",
    "httpx",
    "requests",
    "urllib",
    "http",
    "socket",
    "aiohttp",
)


@pytest.mark.parametrize("library", FORBIDDEN_IMPORTS)
def test_the_inference_layer_reaches_no_database_and_no_network(library: str) -> None:
    pattern = re.compile(rf"^\s*(import|from)\s+{re.escape(library)}\b", re.MULTILINE)
    for name in OFFLINE_MODULES:
        source = (REPOSITORY_ROOT / name).read_text(encoding="utf-8")
        assert not pattern.search(source), f"{name}: {library}"


def test_no_offline_module_imports_the_running_application() -> None:
    """`app.ml.dataset` is the pure Stage 6.1 contract. Everything else under `app.` is the
    service, and nothing in `ml/` may reach it."""
    pattern = re.compile(r"^\s*(?:from|import)\s+(app(?:\.[\w.]+)?)", re.MULTILINE)
    for path in sorted((REPOSITORY_ROOT / "ml").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for module in pattern.findall(path.read_text(encoding="utf-8")):
            assert module == "app.ml.dataset", f"{path.name} imports {module}"


def test_exactly_one_application_module_can_reach_an_artifact() -> None:
    """Stage 6.5 asserted that none did; Stage 6.6 introduced one, and this names it.

    The guard is re-pointed rather than removed, and the claim it defends is sharpened rather
    than widened. What made it worth having was never the number zero -- it was that the set of
    modules **able to unpickle an artifact** is enumerated, so a second one has to be argued for
    in the commit that adds it.

    Stage 6.9 added a second importer of ``ml``, and it is deliberately not a second importer of
    *this*: ``app/ml/accuracy.py`` reaches ``ml.metrics``, which is arithmetic over floats with
    no artifact, no pickle and no estimator anywhere in it. So the two claims are separated
    here: the artifact-reaching set is still exactly one module, and the broader allowlist of
    who may reach ``ml`` at all is pinned by
    ``tests/backend/test_ml_serving.py::test_each_bridge_reaches_only_its_own_offline_module``.
    """
    application = REPOSITORY_ROOT / "backend" / "app"
    sources = {
        path.relative_to(application).as_posix(): path.read_text(encoding="utf-8")
        for path in application.rglob("*.py")
        if "__pycache__" not in path.parts
    }

    artifact_reaching = re.compile(r"^\s*(?:from|import)\s+ml\.(?:artifact|inference)\b", re.M)
    assert sorted(name for name, text in sources.items() if artifact_reaching.search(text)) == [
        "ml/artifact_store.py"
    ]

    any_ml = re.compile(r"^\s*(?:from|import)\s+ml\b", re.MULTILINE)
    assert sorted(name for name, text in sources.items() if any_ml.search(text)) == [
        "ml/accuracy.py",
        "ml/artifact_store.py",
    ]

    # And the new one really is nowhere near a pickle. Read from the AST, not from the text:
    # its docstring names `ml.artifact` in order to explain which precedent it follows, and a
    # substring search would read that explanation as the thing it rules out.
    tree = ast.parse(sources["ml/accuracy.py"])
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    called = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}

    assert not [name for name in imported if "pickle" in name or "artifact" in name]
    assert not [name for name in called if "load_artifact" in name or "predict" in name]


def test_exactly_one_route_serves_the_model_and_none_names_an_artifact() -> None:
    """Stage 6.6 added the serving endpoint. One route SCORES, GET only, no artifact in a path.

    The second half is the part that still matters: a path segment naming an artifact, a file
    or an estimator would be a path a client could vary, and the whole point of the loading
    boundary is that no request chooses what is loaded.

    Stage 6.11 put a second route on the ML router and Stage 7.3 two more, and none of the three
    is a second serving route: each reads or measures rows the serving route already wrote, loads
    no artifact and declares no 503. What this file is responsible for -- that exactly one route
    can reach the model, and that no path names an artifact -- is unchanged, and is asserted as
    that rather than as a route count.
    """
    from app.core.config import Settings
    from app.main import create_app

    schema = create_app(Settings(environment="test", debug=True)).openapi()
    paths = schema["paths"]
    methods = {"get", "post", "put", "patch", "delete", "head", "options"}
    # Stage 7.7's copilot route is outside /ml and reaches the model only through the
    # serving service, as a tool -- still exactly one route that loads the artifact.
    # Stage 7.9's knowledge routes are outside /ml and reach no model at all.
    assert sum(len([m for m in spec if m in methods]) for spec in paths.values()) == 93

    assert sorted(path for path in paths if "/ml/" in path) == sorted(ML_ENDPOINTS)
    for endpoint in ML_ENDPOINTS:
        assert set(paths[endpoint]) == {"get"}, endpoint

    # Only the serving route can fail for want of a model, which is what makes it the only one
    # that reaches one. Asserted over every other ML route rather than over a named list, so a
    # fifth route cannot be added without either declaring no 503 or failing here.
    assert "503" in paths[SERVING_ENDPOINT]["get"]["responses"]
    for endpoint in ML_ENDPOINTS:
        if endpoint == SERVING_ENDPOINT:
            continue
        assert "503" not in paths[endpoint]["get"]["responses"], endpoint
    assert not [
        path
        for path in paths
        if any(word in path.lower() for word in ("artifact", "infer", "pickle", "estimator"))
    ]


# --- 17./20. no hidden training ------------------------------------------------------------------


@pytest.mark.parametrize("method", ["fit(", "fit_predict(", "partial_fit("])
def test_the_inference_source_calls_no_training_method(method: str) -> None:
    source = (REPOSITORY_ROOT / "ml" / "inference.py").read_text(encoding="utf-8")
    assert method not in source, method


def test_inference_never_trains(
    loaded: Any, dataset: ProcessedDataset, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The behavioural version: make training impossible and require inference to work."""

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("inference called a training method")

    for name in ("fit", "partial_fit"):
        if hasattr(HistGradientBoostingRegressor, name):
            monkeypatch.setattr(HistGradientBoostingRegressor, name, explode, raising=False)

    rows = scorable(dataset, loaded.feature_columns)
    predictions = predict_demand(loaded, rows)
    assert len(predictions) == len(rows)
    assert all(math.isfinite(item.prediction) for item in predictions)


def test_inference_does_not_mutate_the_artifact(
    artifact_paths: tuple[Path, Path], dataset: ProcessedDataset
) -> None:
    payload_path, _ = artifact_paths
    before = payload_path.read_bytes()
    loaded = load_artifact(*artifact_paths)
    predict_demand(loaded, scorable(dataset, loaded.feature_columns))
    assert payload_path.read_bytes() == before


# --- 22./23./24. claims and output identity ------------------------------------------------------


def test_every_claim_flag_is_false_in_the_committed_metadata() -> None:
    claims = load_artifact_metadata()["claims"]
    assert isinstance(claims, dict)
    assert claims["production_ready"] is False
    assert claims["production_accuracy_established"] is False
    assert claims["cross_hotel_generalisation_established"] is False
    assert claims["serving_enabled"] is False


def test_the_prediction_carries_no_field_that_could_hold_an_internal_key() -> None:
    names = {field.name for field in fields(DemandPrediction)}
    assert names == {
        "hotel_public_id",
        "target_date",
        "model_version",
        "forecast_horizon_days",
        "prediction",
    }
    for name in names:
        assert not name.endswith("_id") or name == "hotel_public_id"


def test_predictions_identify_a_hotel_by_public_uuid_only(
    loaded: Any, dataset: ProcessedDataset
) -> None:
    predictions = predict_demand(loaded, scorable(dataset, loaded.feature_columns))
    assert predictions
    for item in predictions:
        assert isinstance(item.hotel_public_id, uuid.UUID)
        assert item.hotel_public_id.version == 5  # offline identity, never a production v4
        assert item.model_version == MODEL_VERSION
        assert item.forecast_horizon_days == 7
        assert isinstance(item.prediction, float)


# --- 25. training never reaches held-out data ----------------------------------------------------


def test_the_training_partition_ends_before_the_held_out_data_starts(
    dataset: ProcessedDataset, trained: TrainedArtifact
) -> None:
    held_out = min(row.target_date for row in dataset.rows if row.partition in HELD_OUT_PARTITIONS)
    assert trained.training_end < held_out
    assert TRAINING_PARTITION == "train"


def test_a_training_set_containing_a_held_out_row_is_refused(
    dataset: ProcessedDataset,
) -> None:
    rows = [*training_rows(dataset), next(r for r in dataset.rows if r.partition == "test")]
    with pytest.raises(ArtifactError, match="partition"):
        assert_no_held_out_row(rows, dataset)


def test_training_rows_are_only_the_declared_partition(dataset: ProcessedDataset) -> None:
    rows = training_rows(dataset)
    assert {row.partition for row in rows} == {TRAINING_PARTITION}
    assert len(rows) == PARTITION_ROWS


# --- row accounting: three different numbers, three different names -----------------------------


#: The declared training partition, before any feature-validity filtering.
PARTITION_ROWS = 872
#: Rows of that partition with every selected feature present -- what `design_matrix` keeps.
FEATURE_VALID_ROWS = 816
#: Rows held out because `demand_lag_28` does not exist yet: 28 dates x 2 hotels.
ROWS_WITHOUT_FEATURE_HISTORY = 56


def test_the_three_row_counts_are_distinct_and_add_up(dataset: ProcessedDataset) -> None:
    """872 rows enter the selection; 816 reach the estimator. Both numbers are real.

    They are not interchangeable, and the earlier Stage 6.5 report used one word for both.
    `Fold.train_rows` in the evaluation protocol is the PRE-filter count; `fitted_rows` on the
    artifact is the POST-filter count.
    """
    rows = training_rows(dataset)
    matrix = design_matrix(rows, EXPECTED_FEATURE_COLUMNS)
    assert len(rows) == PARTITION_ROWS
    assert len(matrix.used) == FEATURE_VALID_ROWS
    assert len(matrix.skipped) == ROWS_WITHOUT_FEATURE_HISTORY
    assert len(matrix.used) + len(matrix.skipped) == len(rows)
    assert len(matrix.features) == len(matrix.targets) == FEATURE_VALID_ROWS
    # Every skipped row is skipped for the one documented reason.
    for index in matrix.skipped:
        assert rows[index].features["demand_lag_28"] is None


def test_the_artifact_reports_the_post_filter_count_as_its_training_rows(
    trained: TrainedArtifact,
) -> None:
    assert trained.partition_rows == PARTITION_ROWS
    assert trained.fitted_rows == FEATURE_VALID_ROWS
    assert trained.held_out_for_missing_features == ROWS_WITHOUT_FEATURE_HISTORY
    assert trained.model.training_rows == FEATURE_VALID_ROWS
    assert trained.training_start == dt.date(2015, 9, 23)
    assert trained.training_end == dt.date(2016, 11, 9)
    assert len(trained.training_hotels) == 2
    assert trained.training_dates == 414


def test_the_number_that_reaches_the_estimator_is_the_post_filter_one(
    dataset: ProcessedDataset, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured at the call boundary, not inferred from a dataclass field."""
    captured: list[int] = []
    original = HistGradientBoostingRegressor.fit

    def recording_fit(self: Any, X: Any, y: Any, **kwargs: Any) -> Any:  # noqa: N803
        captured.append(len(X))
        return original(self, X, y, **kwargs)

    monkeypatch.setattr(HistGradientBoostingRegressor, "fit", recording_fit)
    train_artifact(dataset, dataset_version="v1", feature_version="v1")
    assert captured == [FEATURE_VALID_ROWS]


def test_training_rows_are_in_the_evaluation_protocols_order(dataset: ProcessedDataset) -> None:
    """Not the dataset file's order -- the two disagree within a date."""
    assert TRAINING_ROW_ORDER == ("target_date", "hotel_key")
    rows = training_rows(dataset)
    assert list(rows) == sorted(rows, key=lambda row: (row.target_date, row.hotel_key))

    file_order = [row for row in dataset.rows if row.partition == TRAINING_PARTITION]
    assert {(r.hotel_key, r.target_date) for r in rows} == {
        (r.hotel_key, r.target_date) for r in file_order
    }
    # The orders really are different, so the sort above is doing work.
    assert [(r.hotel_key, r.target_date) for r in rows] != [
        (r.hotel_key, r.target_date) for r in file_order
    ]


def test_the_estimator_is_row_order_invariant_for_this_configuration(
    dataset: ProcessedDataset,
) -> None:
    """Why the equivalence held even before the orders were aligned -- recorded, not relied on.

    The artifact now feeds the estimator the protocol's row order, so the equivalence no longer
    depends on this. Keeping the measurement means a scikit-learn change that broke it would be
    visible here rather than as a mysterious digest drift.
    """
    ordered = training_rows(dataset)
    shuffled = sorted(ordered, key=lambda row: (row.target_date, str(row.hotel_public_id)))
    assert list(ordered) != shuffled
    config = EstimatorConfig()
    first = LearnedModel.fit(list(ordered), EXPECTED_FEATURE_COLUMNS, config)
    second = LearnedModel.fit(shuffled, EXPECTED_FEATURE_COLUMNS, config)
    assert first.training_rows == second.training_rows == FEATURE_VALID_ROWS
    assert probe_predictions(first.estimator) == probe_predictions(second.estimator)


def test_the_metadata_names_the_two_counts_apart(artifact_paths: tuple[Path, Path]) -> None:
    training = json.loads(artifact_paths[1].read_text(encoding="utf-8"))["training"]
    assert training["partition_rows"] == PARTITION_ROWS
    assert training["training_row_count"] == FEATURE_VALID_ROWS
    assert training["held_out_for_missing_features"] == ROWS_WITHOUT_FEATURE_HISTORY
    assert training["row_order"] == list(TRAINING_ROW_ORDER)
    assert "different numbers" in str(training["accounting_note"])


def test_a_non_finite_feature_stops_the_fit(dataset: ProcessedDataset) -> None:
    row = next(r for r in dataset.rows if r.features["demand_lag_7"] is not None)
    poisoned = ProcessedRow(
        hotel_key=row.hotel_key,
        hotel_public_id=row.hotel_public_id,
        target_date=row.target_date,
        horizon_days=row.horizon_days,
        prediction_cutoff=row.prediction_cutoff,
        partition="train",
        target_room_nights=row.target_room_nights,
        features={**row.features, "demand_lag_7": float("nan")},
    )
    with pytest.raises(ArtifactError, match="non-finite"):
        assert_all_finite([poisoned], EXPECTED_FEATURE_COLUMNS)


# --- 26./27./28. the metadata points where it claims ---------------------------------------------


def test_the_metadata_records_the_exact_estimator_configuration(
    artifact_paths: tuple[Path, Path],
) -> None:
    metadata = json.loads(artifact_paths[1].read_text(encoding="utf-8"))
    model = metadata["model"]
    assert model["estimator_configuration_sha256"] == STAGE_63_ESTIMATOR_SHA256
    assert model["estimator_configuration"] == EstimatorConfig().as_dict()
    assert model["model_name"] == MODEL_NAME


def test_the_metadata_records_the_exact_dataset_and_feature_contract(
    artifact_paths: tuple[Path, Path], dataset: ProcessedDataset
) -> None:
    metadata = json.loads(artifact_paths[1].read_text(encoding="utf-8"))
    block = metadata["dataset"]
    assert block["dataset_sha256"] == dataset.sha256
    assert block["dataset_version"] == "v1"
    assert block["feature_version"] == "v1"
    assert tuple(block["feature_columns"]) == EXPECTED_FEATURE_COLUMNS


def test_the_metadata_records_the_training_extent(
    artifact_paths: tuple[Path, Path], trained: TrainedArtifact
) -> None:
    training = json.loads(artifact_paths[1].read_text(encoding="utf-8"))["training"]
    assert training["partition"] == TRAINING_PARTITION
    assert training["training_row_count"] == trained.fitted_rows == 816
    assert training["partition_rows"] == 872
    assert training["held_out_for_missing_features"] == 56
    assert training["training_hotel_count"] == 2
    assert training["training_date_count"] == 414
    assert training["training_start_date"] == "2015-09-23"
    assert training["training_end_date"] == "2016-11-09"


# --- 29. the reproducibility digest --------------------------------------------------------------


def test_the_canonical_digest_is_stable_across_two_independent_fits(
    dataset: ProcessedDataset, trained: TrainedArtifact
) -> None:
    again = train_artifact(dataset, dataset_version="v1", feature_version="v1")
    assert canonical_model_digest(again) == canonical_model_digest(trained)
    assert canonical_model_digest(trained) == CANONICAL_DIGEST


def test_the_committed_metadata_carries_the_same_canonical_digest() -> None:
    artifact = load_artifact_metadata()["artifact"]
    assert isinstance(artifact, dict)
    assert artifact["canonical_model_digest"] == CANONICAL_DIGEST


def test_a_different_configuration_moves_the_canonical_digest(
    dataset: ProcessedDataset, trained: TrainedArtifact
) -> None:
    other = train_artifact(
        dataset,
        dataset_version="v1",
        feature_version="v1",
        config=EstimatorConfig(max_iter=20),
    )
    assert canonical_model_digest(other) != canonical_model_digest(trained)


def test_the_probe_grid_is_deterministic_and_shaped_like_the_feature_vector() -> None:
    grid = probe_matrix()
    assert grid == probe_matrix()
    assert len(grid) == PROBE_ROWS
    assert all(len(row) == len(EXPECTED_FEATURE_COLUMNS) for row in grid)
    assert all(math.isfinite(value) for row in grid for value in row)


def test_the_probe_predictions_are_formatted_to_a_fixed_precision(trained: TrainedArtifact) -> None:
    values = probe_predictions(trained.estimator)
    assert len(values) == PROBE_ROWS
    assert all(re.fullmatch(rf"-?\d+\.\d{{{PROBE_DIGITS}}}", value) for value in values)


# --- the registry block --------------------------------------------------------------------------


def test_the_registry_block_is_a_pointer_and_declares_what_it_is(
    artifact_paths: tuple[Path, Path],
) -> None:
    metadata = json.loads(artifact_paths[1].read_text(encoding="utf-8"))
    block = registry_artifact_block(metadata, metadata_filename="artifact.json")
    assert block["record_kind"] == "MODEL ARTIFACT METADATA"
    assert block["serving_enabled"] is False
    assert block["production_ready"] is False
    assert block["committed"] is False
    assert block["metadata_file"] == "artifact.json"
    assert "probe_predictions" not in block  # a pointer, not a second copy


def test_the_training_guard_refuses_a_dataset_without_a_training_partition(
    dataset: ProcessedDataset,
) -> None:
    empty = ProcessedDataset(
        rows=tuple(r for r in dataset.rows if r.partition != "train")[:10],
        feature_names=dataset.feature_names,
        sha256=dataset.sha256,
        size_bytes=dataset.size_bytes,
        path=dataset.path,
    )
    with pytest.raises(ArtifactError, match="no rows in the 'train' partition"):
        training_rows(empty)


def test_the_trainer_refuses_a_model_version_it_does_not_implement(
    dataset: ProcessedDataset,
) -> None:
    with pytest.raises(ValidationError, match="version mismatch"):
        train_artifact(
            dataset,
            dataset_version="v1",
            feature_version="v1",
            model_version="demand_baseline_v9",
        )
