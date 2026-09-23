"""The Stage 6.3 backtest, end to end, against the committed Stage 6.2 dataset.

Offline in every sense: no network, no PostgreSQL, no demo database, no fixture invented here.
It reads the dataset and the manifest that Stage 6.2 committed, checks the one against the
other, and runs the real 54-fold evaluation.

Three things this file checks that the unit tests cannot:

* the **committed** dataset still hashes to the value its **committed** manifest claims;
* the evaluation is reproducible on this machine -- run twice, same content checksum;
* nothing in this stage leaked an ML dependency into the application package, and the public
  API and migration chain are exactly where V1 left them.

The learned model's metrics are compared against the committed record with a stated tolerance
rather than for exact equality. A compiled tree ensemble is bit-identical for a fixed build --
verified here across thread counts -- but that guarantee does not extend across compilers, and
an assertion that pretends otherwise would fail for a reason that has nothing to do with the
model. The **baseline** is pure Python arithmetic, so it is compared exactly.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from sklearn.ensemble import HistGradientBoostingRegressor

from ml.artifact import (
    build_artifact_metadata,
    load_artifact,
    load_artifact_metadata,
    serialise_model,
    train_artifact,
    training_rows,
)
from ml.evaluation import EvaluationResult, evaluate, per_hotel_metrics
from ml.inference import FeatureVector, predict_demand
from ml.loading import (
    DEFAULT_DATASET,
    DEFAULT_DATASET_MANIFEST,
    ProcessedDataset,
    load_dataset_manifest,
    load_processed_dataset,
    require_versions,
    verify_against_manifest,
)
from ml.manifests import (
    DEFAULT_EVALUATION_RECORD,
    build_evaluation_manifest,
    content_checksum,
)
from ml.models import MODEL_VERSION, design_matrix
from ml.policy import ACCEPTANCE_POLICY, ACCEPTANCE_POLICY_VERSION
from ml.registry import without_artifact
from ml.validation import assert_fold_boundaries_match, leakage_report
from tests.ml.test_demand_model_validation import (
    STAGE_63_ESTIMATOR_SHA256,
    STAGE_63_PROTOCOL_SHA256,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

#: Room nights. The learned metrics must land within this of the committed record; anything
#: further apart is a real change, not a floating-point difference between toolchains.
METRIC_TOLERANCE = 0.5


@pytest.fixture(scope="module")
def dataset() -> ProcessedDataset:
    return load_processed_dataset(DEFAULT_DATASET)


@pytest.fixture(scope="module")
def manifest() -> dict[str, object]:
    return load_dataset_manifest(DEFAULT_DATASET_MANIFEST)


@pytest.fixture(scope="module")
def result(dataset: ProcessedDataset) -> EvaluationResult:
    """One full backtest, shared across this module. It costs about twelve seconds."""
    return evaluate(dataset.rows, feature_names=dataset.feature_names)


@pytest.fixture(scope="module")
def record() -> dict[str, object]:
    assert DEFAULT_EVALUATION_RECORD.is_file(), (
        f"{DEFAULT_EVALUATION_RECORD} is missing; regenerate it with "
        "`python -m ml.pipelines.evaluate_demand_model`"
    )
    loaded = json.loads(DEFAULT_EVALUATION_RECORD.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


# --- the committed dataset is the one the committed manifest describes --------------------------


def test_the_committed_dataset_matches_its_committed_manifest(
    dataset: ProcessedDataset, manifest: dict[str, object]
) -> None:
    verify_against_manifest(dataset, manifest)
    require_versions(manifest, dataset_version="v1", feature_version="v1")


def test_the_dataset_is_the_one_stage_62_measured(dataset: ProcessedDataset) -> None:
    assert len(dataset.rows) == 1462
    assert dataset.hotel_keys == ("city_hotel", "resort_hotel")
    assert dataset.dates[0] == dt.date(2015, 8, 26)
    assert dataset.dates[-1] == dt.date(2017, 8, 31)
    assert len(dataset.dates) == 737


def test_the_dataset_checkout_has_not_been_line_ending_mangled(
    dataset: ProcessedDataset,
) -> None:
    """`.gitattributes` pins this file to `-text`; without it the digest would be per-platform."""
    assert b"\r" not in DEFAULT_DATASET.read_bytes()


def test_capacity_is_still_absent_on_every_row(dataset: ProcessedDataset) -> None:
    assert all(row.features["rooms_existing_at_cutoff"] is None for row in dataset.rows)


# --- the backtest ---------------------------------------------------------------------------------


def test_the_backtest_covers_every_date_after_the_first_origin_exactly_once(
    result: EvaluationResult,
) -> None:
    assert len(result.folds) == 54
    assert result.skipped_folds == ()
    seen = [(p.hotel_key, p.target_date) for p in result.predictions]
    assert len(seen) == len(set(seen)) == 744


def test_no_fold_trains_on_a_date_it_evaluates(result: EvaluationResult) -> None:
    for fold in result.folds:
        assert fold.fold.train_end <= fold.fold.origin
        assert fold.fold.origin < fold.fold.evaluation_start
        assert fold.fold.evaluation_end <= fold.fold.origin + dt.timedelta(
            days=result.policy.horizon_days
        )


def test_the_training_window_expands_and_never_contracts(result: EvaluationResult) -> None:
    sizes = [fold.fold.train_rows for fold in result.folds]
    assert sizes == sorted(sizes)
    assert sizes[0] < sizes[-1]


def test_only_the_final_fold_has_a_truncated_window(result: EvaluationResult) -> None:
    incomplete = [fold.fold.index for fold in result.folds if not fold.fold.complete_window]
    assert incomplete == [result.folds[-1].fold.index]


def test_every_observation_was_forecastable_by_both_methods(
    result: EvaluationResult,
) -> None:
    """Measured, not assumed. A skipped prediction would be reported, not silently dropped."""
    assert result.baseline_pooled.skipped == 0
    assert result.learned_pooled.skipped == 0
    assert result.baseline_pooled.observations == 744
    assert result.learned_pooled.observations == 744


def test_the_selected_features_are_the_nine_knowable_seven_days_ahead(
    result: EvaluationResult,
) -> None:
    assert result.selection.selected == (
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
    excluded = dict(result.selection.excluded)
    assert set(excluded) == {
        "demand_lag_1",
        "demand_rolling_mean_7",
        "demand_rolling_mean_14",
        "demand_rolling_mean_28",
        "on_books_room_nights_at_cutoff",
        "rooms_existing_at_cutoff",
    }


def test_both_hotels_have_enough_days_for_a_reported_metric(
    result: EvaluationResult,
) -> None:
    per_hotel = per_hotel_metrics(result.predictions)
    assert set(per_hotel) == {"city_hotel", "resort_hotel"}
    for block in per_hotel.values():
        assert isinstance(block, dict)
        assert block["sufficient_coverage"] is True
        assert block["predictions"] == 372


# --- reproducibility ---------------------------------------------------------------------------


def test_running_the_evaluation_twice_gives_the_same_numbers(
    dataset: ProcessedDataset, result: EvaluationResult
) -> None:
    again = evaluate(dataset.rows, feature_names=dataset.feature_names)
    assert again.predictions == result.predictions
    assert again.baseline_pooled == result.baseline_pooled
    assert again.learned_pooled == result.learned_pooled

    stamp = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    left = build_evaluation_manifest(
        result,
        dataset_version="v1",
        feature_version="v1",
        dataset_sha256=dataset.sha256,
        dataset_rows=len(dataset.rows),
        dataset_path=DEFAULT_DATASET.name,
        generated_at=stamp,
    )
    right = build_evaluation_manifest(
        again,
        dataset_version="v1",
        feature_version="v1",
        dataset_sha256=dataset.sha256,
        dataset_rows=len(dataset.rows),
        dataset_path=DEFAULT_DATASET.name,
        generated_at=stamp + dt.timedelta(days=400),
    )
    assert content_checksum(left) == content_checksum(right)


# --- the committed evaluation record -----------------------------------------------------------


def test_the_record_describes_this_dataset_and_this_model(
    record: dict[str, object], dataset: ProcessedDataset
) -> None:
    model = record["model"]
    block = record["dataset"]
    assert isinstance(model, dict)
    assert isinstance(block, dict)
    assert model["model_version"] == MODEL_VERSION
    assert model["model_artifact_persisted"] is False
    assert block["sha256"] == dataset.sha256
    assert block["rows"] == len(dataset.rows)


def test_the_recorded_baseline_metrics_reproduce_exactly(
    record: dict[str, object], result: EvaluationResult
) -> None:
    """Pure Python arithmetic over integers: there is no tolerance to allow here."""
    metrics = record["metrics"]
    assert isinstance(metrics, dict)
    recorded = metrics["pooled"]["baseline"]
    pooled = result.baseline_pooled.as_dict()
    assert recorded == pooled


def test_the_recorded_learned_metrics_reproduce_within_tolerance(
    record: dict[str, object], result: EvaluationResult
) -> None:
    metrics = record["metrics"]
    assert isinstance(metrics, dict)
    recorded = metrics["pooled"]["learned"]
    pooled = result.learned_pooled.as_dict()
    assert recorded["observations"] == pooled["observations"]
    assert recorded["skipped"] == pooled["skipped"]
    for name in ("mae", "rmse", "smape"):
        assert pooled[name] == pytest.approx(recorded[name], abs=METRIC_TOLERANCE), name


def test_the_record_reports_the_difference_and_declares_no_winner(
    record: dict[str, object],
) -> None:
    text = json.dumps(record).lower()
    for marketing in ("winner", "beats", "outperform", "production-ready", "accurate"):
        assert marketing not in text, marketing
    metrics = record["metrics"]
    assert isinstance(metrics, dict)
    difference = metrics["pooled"]["comparison"]["learned_minus_baseline"]
    assert set(difference) == {"mae", "rmse", "smape"}


# --- nothing leaked into the application ---------------------------------------------------------


FORBIDDEN_IN_BACKEND = (
    "sklearn",
    "scikit_learn",
    "numpy",
    "scipy",
    "pandas",
    "joblib",
    "torch",
    "tensorflow",
)

FORBIDDEN_EVERYWHERE = (
    "xgboost",
    "lightgbm",
    "catboost",
    "torch",
    "tensorflow",
    "keras",
    "prophet",
    "statsmodels",
    "langchain",
    "openai",
    "anthropic",
    "transformers",
    "sentence_transformers",
    "chromadb",
    "faiss",
)


def _python_sources(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


@pytest.mark.parametrize("library", FORBIDDEN_IN_BACKEND)
def test_the_application_package_gained_no_ml_dependency(library: str) -> None:
    """The API image must stay what it was. `ml/` is excluded from its build context and the
    backend requirements were not touched -- this asserts the code agrees."""
    pattern = re.compile(rf"^\s*(import|from)\s+{re.escape(library)}\b", re.MULTILINE)
    for source in _python_sources(REPOSITORY_ROOT / "backend" / "app"):
        assert not pattern.search(source.read_text(encoding="utf-8")), f"{source}: {library}"


@pytest.mark.parametrize("library", FORBIDDEN_EVERYWHERE)
def test_no_forbidden_library_appears_anywhere_in_the_repository(library: str) -> None:
    pattern = re.compile(rf"^\s*(import|from)\s+{re.escape(library)}\b", re.MULTILINE)
    for root in ("backend/app", "ml"):
        for source in _python_sources(REPOSITORY_ROOT / root):
            assert not pattern.search(source.read_text(encoding="utf-8")), f"{source}: {library}"


def test_the_backend_requirements_declare_exactly_one_ml_library() -> None:
    """Stage 6.3 asserted none. Stage 6.7 adds one, and this says which and no more.

    The production image has to deserialise the approved artifact and call `predict`, which
    needs scikit-learn importable. Nothing else was added: NumPy, SciPy, joblib and
    threadpoolctl arrive as its own requirements rather than as declarations here, and pandas
    is installed nowhere.

    Comments are stripped first. requirements.txt DISCUSSES pandas at length -- to say it is not
    installed -- and a substring search would read that as an installation.
    """

    def declared(name: str) -> list[str]:
        text = (REPOSITORY_ROOT / "backend" / name).read_text(encoding="utf-8")
        return [
            line.strip().lower()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    runtime = declared("requirements.txt")
    assert [line for line in runtime if line.startswith("scikit-learn")] == ["scikit-learn==1.9.1"]
    for library in ("sklearn", "numpy", "scipy", "pandas", "torch"):
        assert not [line for line in runtime if line.startswith(library)], (library, runtime)

    development = declared("requirements-dev.txt")
    for library in ("scikit-learn", "sklearn", "numpy", "scipy", "pandas", "torch"):
        assert not [line for line in development if line.startswith(library)], library


def test_scikit_learn_is_declared_once_and_pinned() -> None:
    text = (REPOSITORY_ROOT / "ml" / "requirements-ml.txt").read_text(encoding="utf-8")
    pins = re.findall(r"^scikit-learn==\d+\.\d+\.\d+$", text, re.MULTILINE)
    assert len(pins) == 1, text


# --- the public API and the schema are where V1 left them ----------------------------------------


def test_the_public_api_gained_only_the_serving_endpoint() -> None:
    """Stage 6.3 asserted 82 operations and no ML path at all. Stage 6.6 added one route.

    What this still holds is the part Stage 6.3 cared about: no training surface. There is no
    route that fits a model, launches a run or promotes an artifact, and every ML route that now
    exists is a read.
    """
    from app.core.config import Settings
    from app.main import create_app

    serving = "/api/v1/hotels/{hotel_public_id}/ml/demand-forecast"
    schema = create_app(Settings(environment="test", debug=True)).openapi()
    paths = schema["paths"]
    methods = {"get", "post", "put", "patch", "delete", "head", "options"}
    operations = sum(len([m for m in spec if m in methods]) for spec in paths.values())
    assert operations == 86
    # Four routes on the ML prefix since Stage 7.3, and only one of them reaches a model. The
    # 503 is what says which: an unavailable artifact is a failure only the serving route can
    # have, so declaring it is the structural difference rather than a naming convention.
    ml_paths = sorted(path for path in paths if "/ml" in path)
    assert ml_paths == [
        serving,
        "/api/v1/hotels/{hotel_public_id}/ml/demand-predictions",
        "/api/v1/hotels/{hotel_public_id}/ml/forecast-accuracy",
        "/api/v1/hotels/{hotel_public_id}/ml/prediction-distribution",
    ]
    assert "503" in paths[serving]["get"]["responses"]
    for path in ml_paths:
        assert set(paths[path]) == {"get"}, path
        if path != serving:
            assert "503" not in paths[path]["get"]["responses"], path
    assert not [
        path for path in paths if any(word in path for word in ("train", "fit", "forecast/run"))
    ]


def test_the_migration_chain_is_unchanged() -> None:
    versions = REPOSITORY_ROOT / "database" / "migrations" / "versions"
    revisions = sorted(p.name for p in versions.glob("*.py"))
    # Stage 6.8 added the tenth, for `demand_predictions`. What this file is responsible for is
    # that the ML work of Stage 6.3 needed no schema change of its own, and it still did not.
    assert len(revisions) == 11
    assert revisions[-1].endswith("0011_demand_prediction_public_id.py")


#: Committed beside the model: four JSON records. `model.pkl` is the Stage 6.5 payload, which is
#: generated rather than committed -- it may or may not be present on a given machine, and it
#: must never be in the repository.
COMMITTED_MODEL_RECORDS = ("artifact.json", "metrics.json", "registry.json", "validation.json")
GENERATED_MODEL_PAYLOAD = "model.pkl"


def test_only_json_records_are_committed_beside_the_model() -> None:
    """No weights in the repository. The payload is generated; everything tracked is readable."""
    directory = DEFAULT_EVALUATION_RECORD.parent
    assert directory.is_dir()
    present = sorted(item.name for item in directory.iterdir() if item.is_file())
    assert set(COMMITTED_MODEL_RECORDS) <= set(present), present
    unexpected = [
        name for name in present if name not in (*COMMITTED_MODEL_RECORDS, GENERATED_MODEL_PAYLOAD)
    ]
    assert unexpected == [], unexpected
    for name in present:
        assert not name.endswith((".joblib", ".onnx", ".h5", ".pt", ".pb", ".bin")), name


# --- Stage 6.4: the committed validation and registry records -------------------------------------


@pytest.fixture(scope="module")
def validation_record() -> dict[str, object]:
    path = DEFAULT_EVALUATION_RECORD.parent / "validation.json"
    assert path.is_file(), (
        f"{path} is missing; regenerate it with `python -m ml.pipelines.validate_demand_model`"
    )
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.fixture(scope="module")
def registry_record() -> dict[str, object]:
    path = DEFAULT_EVALUATION_RECORD.parent / "registry.json"
    assert path.is_file(), (
        f"{path} is missing; regenerate it with `python -m ml.pipelines.validate_demand_model`"
    )
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_the_committed_validation_reproduces_this_runs_fold_boundaries(
    validation_record: dict[str, object], result: EvaluationResult
) -> None:
    folds = validation_record["folds"]
    assert isinstance(folds, dict)
    assert_fold_boundaries_match(result, folds["boundaries"])
    assert folds["count"] == 54
    assert folds["skipped"] == []
    assert folds["incomplete_windows"] == [53]


def test_the_committed_validation_records_a_passing_leakage_re_check(
    validation_record: dict[str, object],
) -> None:
    leakage = validation_record["leakage"]
    assert isinstance(leakage, dict)
    assert leakage["passed"] is True
    checks = leakage["checks"]
    assert isinstance(checks, dict)
    assert all(checks.values()), checks
    assert leakage_report(_fresh_result()) == leakage


def _fresh_result() -> EvaluationResult:
    dataset = load_processed_dataset(DEFAULT_DATASET)
    return evaluate(dataset.rows, feature_names=dataset.feature_names)


def test_the_registry_pins_the_stage_63_protocol_and_configuration(
    registry_record: dict[str, object],
) -> None:
    protocol = registry_record["protocol"]
    model = registry_record["model"]
    assert isinstance(protocol, dict)
    assert isinstance(model, dict)
    assert protocol["sha256"] == STAGE_63_PROTOCOL_SHA256
    assert model["configuration_sha256"] == STAGE_63_ESTIMATOR_SHA256
    assert protocol["folds"] == 54
    assert protocol["paired_observations"] == 744
    assert protocol["training_data_range"] == {"start": "2015-08-26", "end": "2017-08-30"}
    assert protocol["evaluation_data_range"] == {"start": "2016-08-25", "end": "2017-08-31"}


def test_the_registry_records_the_acceptance_result_and_its_policy(
    registry_record: dict[str, object],
) -> None:
    acceptance = registry_record["acceptance"]
    assert isinstance(acceptance, dict)
    assert acceptance["policy_version"] == ACCEPTANCE_POLICY_VERSION
    assert acceptance["policy_sha256"] == ACCEPTANCE_POLICY.checksum()
    assert acceptance["result"] == "PASS"
    assert acceptance["criteria_failed"] == []
    assert acceptance["criteria_passed"] == acceptance["criteria_total"] == 13


def test_the_registry_describes_the_committed_dataset(
    registry_record: dict[str, object], dataset: ProcessedDataset
) -> None:
    block = registry_record["dataset"]
    model = registry_record["model"]
    assert isinstance(block, dict)
    assert isinstance(model, dict)
    assert block["dataset_sha256"] == dataset.sha256
    assert block["rows"] == len(dataset.rows)
    assert block["dataset_version"] == "v1"
    assert block["feature_version"] == "v1"
    assert model["model_version"] == MODEL_VERSION
    # Stage 6.5 fitted and persisted an artifact, so this flag flipped -- and its companion
    # records the other half: the payload is written to disk and deliberately not committed.
    assert model["artifact_persisted"] is True
    assert model["artifact_committed"] is False
    assert model["serving_path"] is None


def test_the_validation_checksum_in_the_registry_matches_the_committed_record(
    registry_record: dict[str, object], validation_record: dict[str, object]
) -> None:
    verification = registry_record["verification"]
    assert isinstance(verification, dict)
    assert verification["validation_sha256"] == content_checksum(validation_record)
    assert verification["deterministic"] is True
    assert verification["leakage_checks_passed"] is True


# --- Stage 6.5: the artifact, and what it must not have disturbed ---------------------------------


@pytest.fixture(scope="module")
def artifact(dataset: ProcessedDataset, tmp_path_factory: pytest.TempPathFactory) -> Any:
    """A real artifact, built from the committed dataset into a temporary directory."""
    directory = tmp_path_factory.mktemp("stage65")
    trained = train_artifact(dataset, dataset_version="v1", feature_version="v1")
    payload = serialise_model(trained.estimator)
    payload_path = directory / "model.pkl"
    payload_path.write_bytes(payload)
    metadata_path = directory / "artifact.json"
    metadata_path.write_bytes(
        json.dumps(
            build_artifact_metadata(
                trained,
                artifact_sha256=hashlib.sha256(payload).hexdigest(),
                artifact_bytes=len(payload),
                artifact_filename=payload_path.name,
                dataset_path=DEFAULT_DATASET.name,
                validation_sha256="v" * 64,
                registry_sha256="r" * 64,
                created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            ),
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
    )
    return load_artifact(payload_path, metadata_path)


def test_the_artifact_reproduces_the_stage_63_fold_it_shares_a_training_set_with(
    artifact: Any, dataset: ProcessedDataset, result: EvaluationResult
) -> None:
    """The exact equivalence check.

    Fold 11's origin is 2016-11-09, which is the last date of the declared training partition --
    so that fold's model was fitted on precisely the rows the artifact was fitted on. Its
    fourteen learned predictions are therefore reproducible **exactly**, not within a tolerance,
    and anything less would mean the artifact is not the model Stage 6.3 measured.
    """
    fold = next(f for f in result.folds if f.fold.origin == dt.date(2016, 11, 9))
    # `Fold.train_rows` is the PRE-filter count: the rows handed to `LearnedModel.fit`, before
    # rows with a missing selected feature are held out. 816 of these 872 reach the estimator,
    # which `test_the_two_paths_hand_the_estimator_the_same_matrix` measures at the call itself.
    assert fold.fold.train_rows == PARTITION_ROWS
    partition = {(r.hotel_key, r.target_date) for r in dataset.rows if r.partition == "train"}
    trained_on = {
        (r.hotel_key, r.target_date) for r in dataset.rows if r.target_date <= fold.fold.origin
    }
    assert partition == trained_on

    rows = [r for r in dataset.rows if fold.fold.origin < r.target_date <= fold.fold.evaluation_end]
    predictions = predict_demand(
        artifact,
        [
            FeatureVector(
                hotel_public_id=r.hotel_public_id,
                target_date=r.target_date,
                forecast_horizon_days=7,
                features={
                    name: float(r.features[name])  # type: ignore[arg-type]
                    for name in artifact.feature_columns
                },
            )
            for r in rows
        ],
    )
    recorded = {(p.hotel_key, p.target_date): p.learned for p in fold.predictions}
    assert len(predictions) == 14
    for row, produced in zip(rows, predictions, strict=True):
        assert produced.prediction == recorded[(row.hotel_key, row.target_date)]


def test_the_registry_carries_an_artifact_block_that_points_at_the_committed_metadata(
    registry_record: dict[str, object],
) -> None:
    block = registry_record.get("artifact")
    assert isinstance(block, dict)
    assert block["record_kind"] == "MODEL ARTIFACT METADATA"
    assert block["serving_enabled"] is False
    assert block["production_ready"] is False
    assert block["committed"] is False

    metadata = load_artifact_metadata()
    artifact_block = metadata["artifact"]
    dataset_block = metadata["dataset"]
    assert isinstance(artifact_block, dict)
    assert isinstance(dataset_block, dict)
    assert block["sha256"] == artifact_block["sha256"]
    assert block["canonical_model_digest"] == artifact_block["canonical_model_digest"]
    assert block["dataset_sha256"] == dataset_block["dataset_sha256"]
    assert list(block["feature_columns"]) == list(dataset_block["feature_columns"])


def test_the_stage_64_validation_facts_survived_the_artifact_amendment(
    registry_record: dict[str, object], validation_record: dict[str, object]
) -> None:
    """Amending the registry must not restate a measurement."""
    acceptance = registry_record["acceptance"]
    metrics = registry_record["metrics"]
    claims = registry_record["claims"]
    assert isinstance(acceptance, dict)
    assert isinstance(metrics, dict)
    assert isinstance(claims, dict)
    assert acceptance["result"] == "PASS"
    assert acceptance["criteria_failed"] == []
    assert acceptance["policy_version"] == ACCEPTANCE_POLICY_VERSION
    assert metrics["pooled"]["baseline"]["observations"] == 744
    assert claims["production_ready"] is False
    assert claims["production_accuracy_established"] is False
    assert claims["cross_hotel_generalisation_established"] is False
    assert validation_record["validation_version"] == "validation_v1"


def test_the_artifact_metadata_references_the_stage_64_records_by_checksum(
    registry_record: dict[str, object], validation_record: dict[str, object]
) -> None:
    """The validation record is referenced unconditionally; the registry, as it was at build.

    The two records point at each other, so only one direction can be a checksum of the other.
    The registry reference is therefore the state the artifact was built *against* -- and
    reconstructing that state here proves the amendment changed exactly the artifact block and
    the two model flags, and nothing else.
    """
    metadata = load_artifact_metadata()
    protocol = metadata["protocol"]
    assert isinstance(protocol, dict)
    assert protocol["protocol_sha256"] == STAGE_63_PROTOCOL_SHA256
    assert protocol["acceptance_policy_version"] == ACCEPTANCE_POLICY_VERSION
    assert protocol["acceptance_policy_sha256"] == ACCEPTANCE_POLICY.checksum()
    assert protocol["validation_record_sha256"] == content_checksum(validation_record)

    assert protocol["registry_record_sha256_at_build"] == content_checksum(
        without_artifact(registry_record)
    )


#: Pre-filter: the declared training partition, which is also fold 11's training input.
PARTITION_ROWS = 872
#: Post-filter: the rows that actually reach `HistGradientBoostingRegressor.fit`.
FIT_ROWS = 816


def test_the_two_paths_hand_the_estimator_the_same_matrix(
    dataset: ProcessedDataset, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reproducibility invariant, measured where it actually matters.

    Not "the same rows were selected" but "the same numbers were passed to ``fit``". Both calls
    are captured at the estimator boundary: fold 11 of the real backtest, and the artifact
    build. The feature matrices, the target vectors, the row counts and the row identities must
    all match **as sequences**.

    This is what the earlier report conflated. `Fold.train_rows` is 872 -- the partition handed
    to `LearnedModel.fit` -- while 816 rows survive feature-validity filtering and reach the
    estimator. Both numbers are real; only one of them is a training-row count.
    """
    captured: list[tuple[list[list[float]], list[float]]] = []
    original = HistGradientBoostingRegressor.fit

    def recording_fit(self: Any, X: Any, y: Any, **kwargs: Any) -> Any:  # noqa: N803
        captured.append(([list(row) for row in X], [float(value) for value in y]))
        return original(self, X, y, **kwargs)

    monkeypatch.setattr(HistGradientBoostingRegressor, "fit", recording_fit)

    backtest = evaluate(dataset.rows, feature_names=dataset.feature_names)
    fold_index = next(
        index
        for index, fold in enumerate(backtest.folds)
        if fold.fold.origin == dt.date(2016, 11, 9)
    )
    assert len(captured) == len(backtest.folds) == 54
    fold_x, fold_y = captured[fold_index]

    captured.clear()
    trained = train_artifact(dataset, dataset_version="v1", feature_version="v1")
    assert len(captured) == 1
    artifact_x, artifact_y = captured[0]

    # Counts, named apart.
    assert backtest.folds[fold_index].fold.train_rows == PARTITION_ROWS
    assert trained.partition_rows == PARTITION_ROWS
    assert len(fold_x) == len(artifact_x) == FIT_ROWS
    assert trained.fitted_rows == FIT_ROWS
    assert trained.held_out_for_missing_features == PARTITION_ROWS - FIT_ROWS

    # The matrices themselves.
    assert artifact_x == fold_x
    assert artifact_y == fold_y
    assert all(len(row) == 9 for row in artifact_x)

    # And the identities behind them.
    fold_rows = sorted(
        (r for r in dataset.rows if r.target_date <= dt.date(2016, 11, 9)),
        key=lambda r: (r.target_date, r.hotel_key),
    )
    fold_ids = [
        (fold_rows[i].hotel_key, fold_rows[i].target_date)
        for i in design_matrix(fold_rows, trained.feature_columns).used
    ]
    partition = training_rows(dataset)
    artifact_ids = [
        (partition[i].hotel_key, partition[i].target_date)
        for i in design_matrix(partition, trained.feature_columns).used
    ]
    assert artifact_ids == fold_ids
    assert len(artifact_ids) == FIT_ROWS
    assert min(date for _, date in artifact_ids) == dt.date(2015, 9, 23)
    assert max(date for _, date in artifact_ids) == dt.date(2016, 11, 9)
    assert len({hotel for hotel, _ in artifact_ids}) == 2
    assert len({date for _, date in artifact_ids}) == 414
