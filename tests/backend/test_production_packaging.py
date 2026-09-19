"""Stage 6.7: packaging the approved model for the production image.

Everything here runs without Docker. What needs a container -- the built image's contents, a
real prediction through the production stack, the corrupted-artifact 503 -- is in the
`docker-runtime` job of `.github/workflows/ci.yml`, because a mocked container would prove
nothing about the thing this stage exists to change.

Three groups carry the weight:

* **the refusals** -- every approved value is tampered with in turn and the builder must refuse;
* **identity across independent builds** -- two builds, same canonical digest, same predictions,
  byte-identical metadata, and a payload digest that is allowed to differ and is recorded;
* **the allowlist** -- the Dockerfile's thirteen `ml/` modules are exactly the import closure of
  the two modules the serving boundary reaches for, computed here rather than trusted.
"""

from __future__ import annotations

import ast
import datetime as dt
import hashlib
import json
import re
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.ml import artifact_store
from app.ml.artifact_store import ArtifactLocation
from app.ml.serving import APPROVED_MODEL, build_feature_values
from ml.pipelines.build_production_artifact import (
    STAGE_65_PAYLOAD_SHA256,
    ProductionArtifactError,
    build,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (REPOSITORY_ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
DOCKERIGNORE = (REPOSITORY_ROOT / ".dockerignore").read_text(encoding="utf-8")
REQUIREMENTS = (REPOSITORY_ROOT / "backend" / "requirements.txt").read_text(encoding="utf-8")

APPROVED_METADATA = REPOSITORY_ROOT / "ml" / "models" / "demand_baseline_v1" / "artifact.json"
DATASET = REPOSITORY_ROOT / "ml" / "data" / "processed" / "demand_daily_v1.csv"

CANONICAL_DIGEST = "436bf6b3cc5f1a2cc1a2e7fa5e971cedcd0405aa5b7f07a84967b293118a8f70"
ESTIMATOR_CONFIG_SHA256 = "bbaf2881204f40c67db7a805038042e9c8de902fea8449871bd538c5566d34c5"

TARGET = dt.date(2026, 6, 1)


# --- fixtures -------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def produced(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One production build, exactly as the Docker stage runs it."""
    out = tmp_path_factory.mktemp("production-artifact")
    assert build(out, APPROVED_METADATA, DATASET) == 0
    return out


@pytest.fixture(scope="module")
def second(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An independent second build, for the identity comparison."""
    out = tmp_path_factory.mktemp("production-artifact-b")
    assert build(out, APPROVED_METADATA, DATASET) == 0
    return out


@pytest.fixture(autouse=True)
def isolated_store() -> Iterator[None]:
    artifact_store.configure(None)
    yield
    artifact_store.configure(None)


def metadata_of(directory: Path) -> dict:
    return json.loads((directory / "artifact.json").read_text(encoding="utf-8"))


def predict_with(directory: Path, level: int = 120) -> float:
    artifact_store.configure(
        ArtifactLocation(payload=directory / "model.pkl", metadata=directory / "artifact.json")
    )
    served = artifact_store.approved_model()
    history = {TARGET - dt.timedelta(days=n): level for n in range(7, 29)}
    return artifact_store.predict_room_nights(
        served,
        hotel_public_id=uuid.uuid4(),
        target_date=TARGET,
        features=build_feature_values(history, TARGET),
    )


# --- what the build produces -----------------------------------------------------------------


def test_the_build_produces_a_payload_and_its_metadata(produced: Path) -> None:
    assert (produced / "model.pkl").is_file()
    assert (produced / "artifact.json").is_file()
    assert (produced / "model.pkl").stat().st_size > 100_000


def test_the_identity_is_the_approved_canonical_digest(produced: Path) -> None:
    artifact = metadata_of(produced)["artifact"]

    assert artifact["canonical_model_digest"] == CANONICAL_DIGEST


def test_every_approved_value_survives_into_the_production_metadata(produced: Path) -> None:
    approved = json.loads(APPROVED_METADATA.read_text(encoding="utf-8"))
    made = metadata_of(produced)

    assert made["schema_version"] == approved["schema_version"]
    assert made["model"] == approved["model"]
    assert made["dataset"] == approved["dataset"]
    assert made["training"] == approved["training"]
    assert made["protocol"] == approved["protocol"]
    assert made["claims"] == approved["claims"]
    assert made["model"]["estimator_configuration_sha256"] == ESTIMATOR_CONFIG_SHA256


def test_only_the_two_environment_scoped_fields_differ(produced: Path) -> None:
    """The payload digest and its length. Everything else in the artifact block is approved."""
    approved = json.loads(APPROVED_METADATA.read_text(encoding="utf-8"))["artifact"]
    made = metadata_of(produced)["artifact"]

    differing = {key for key in approved if approved[key] != made.get(key)}

    assert differing <= {"sha256", "bytes"}
    assert made["canonical_model_digest"] == approved["canonical_model_digest"]
    assert made["probe_predictions"] == approved["probe_predictions"]


def test_the_payload_digest_is_recorded_as_a_build_fact_not_a_requirement(produced: Path) -> None:
    made = metadata_of(produced)
    block = made["production_build"]
    on_disk = hashlib.sha256((produced / "model.pkl").read_bytes()).hexdigest()

    assert block["identity"] == "canonical_model_digest"
    assert block["identity_value"] == CANONICAL_DIGEST
    assert block["payload_sha256"] == on_disk == made["artifact"]["sha256"]
    assert block["stage_65_payload_sha256"] == STAGE_65_PAYLOAD_SHA256
    assert isinstance(block["payload_sha256_matches_stage_65"], bool)
    assert "openmp_threads" in block["environment"]


def test_the_pair_is_self_consistent(produced: Path) -> None:
    """What the runtime loader checks before it deserialises anything."""
    made = metadata_of(produced)
    on_disk = (produced / "model.pkl").read_bytes()

    assert made["artifact"]["sha256"] == hashlib.sha256(on_disk).hexdigest()
    assert made["artifact"]["bytes"] == len(on_disk)


def test_the_production_metadata_carries_no_wall_clock(produced: Path) -> None:
    """Two builds of one commit must produce byte-identical files, or the image-reproducibility
    job fails. A timestamp in the production block is the obvious way to break that."""
    block = json.dumps(metadata_of(produced)["production_build"])

    for stamp in ("created_at", "built_at", "timestamp", str(dt.date.today().year) + "-"):
        assert stamp not in block, stamp


# --- identity across independent builds -------------------------------------------------------


def test_two_independent_builds_share_the_canonical_identity(produced: Path, second: Path) -> None:
    a, b = metadata_of(produced), metadata_of(second)

    assert a["artifact"]["canonical_model_digest"] == b["artifact"]["canonical_model_digest"]
    assert a["artifact"]["probe_predictions"] == b["artifact"]["probe_predictions"]


def test_two_independent_builds_predict_identically(produced: Path, second: Path) -> None:
    """The claim that matters operationally: two images, one answer."""
    assert predict_with(produced) == predict_with(second)
    assert predict_with(produced, level=40) == predict_with(second, level=40)


def test_two_independent_builds_write_byte_identical_metadata(produced: Path, second: Path) -> None:
    assert (produced / "artifact.json").read_bytes() == (second / "artifact.json").read_bytes()


def test_the_runtime_accepts_the_production_pair_unchanged(produced: Path) -> None:
    """No Stage 6.6 check is relaxed to make the production artifact loadable."""
    artifact_store.configure(
        ArtifactLocation(payload=produced / "model.pkl", metadata=produced / "artifact.json")
    )
    served = artifact_store.approved_model()

    assert served.model_version == APPROVED_MODEL.model_version
    assert served.feature_columns == APPROVED_MODEL.feature_columns
    assert served.artifact.canonical_model_digest == CANONICAL_DIGEST


def test_the_production_artifact_still_declares_no_serving_approval(produced: Path) -> None:
    claims = metadata_of(produced)["claims"]

    assert claims == {
        "production_ready": False,
        "production_accuracy_established": False,
        "cross_hotel_generalisation_established": False,
        "serving_enabled": False,
        "note": claims["note"],
    }


# --- the refusals ------------------------------------------------------------------------------


def tampered(tmp_path: Path, **changes: object) -> Path:
    """A copy of the approved metadata with one value edited, written where the builder reads."""
    metadata = json.loads(APPROVED_METADATA.read_text(encoding="utf-8"))
    for dotted, value in changes.items():
        block, _, key = dotted.partition("__")
        target = metadata[block] if key else metadata
        target[key or block] = value
    path = tmp_path / "artifact.json"
    path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("change", "what"),
    [
        ({"schema_version": "artifact_v2"}, "schema version"),
        ({"model__model_name": "something_else"}, "model name"),
        ({"model__model_version": "demand_baseline_v2"}, "model version"),
        ({"model__forecast_horizon_days": 14}, "horizon"),
        ({"model__estimator_configuration_sha256": "0" * 64}, "estimator config checksum"),
        ({"dataset__feature_version": "v2"}, "feature version"),
        ({"dataset__dataset_version": "v2"}, "dataset version"),
        ({"dataset__dataset_sha256": "0" * 64}, "dataset checksum"),
        ({"dataset__feature_columns": ["day_of_week"]}, "feature columns"),
        ({"artifact__format": "joblib"}, "artifact format"),
        ({"artifact__canonical_model_digest": "0" * 64}, "canonical digest"),
        ({"artifact__probe_predictions": ["0.000000"]}, "probe predictions"),
        ({"claims__serving_enabled": True}, "a self-authorising artifact"),
        ({"claims__production_ready": True}, "a production-ready claim"),
        ({"training__partition_rows": 871}, "partition rows"),
        ({"training__training_row_count": 815}, "rows reaching fit"),
        ({"training__training_end_date": "2016-11-08"}, "training extent"),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_the_build_refuses_a_model_that_is_not_the_approved_one(
    tmp_path: Path, change: dict[str, object], what: str
) -> None:
    with pytest.raises(ProductionArtifactError):
        build(tmp_path / "out", tampered(tmp_path, **change), DATASET)

    assert not (tmp_path / "out" / "model.pkl").exists(), f"{what}: a payload was written anyway"


def test_the_build_refuses_a_missing_dataset(tmp_path: Path) -> None:
    with pytest.raises(ProductionArtifactError):
        build(tmp_path / "out", APPROVED_METADATA, tmp_path / "absent.csv")


def test_the_build_refuses_a_different_dataset(tmp_path: Path) -> None:
    """A changed training file is a different model, whatever it happens to fit to.

    Which layer refuses it is deliberately not asserted: the loader may reject the file before a
    model exists, or the dataset-checksum comparison may reject it after one does. What is
    asserted is that the build stops and writes nothing.
    """
    altered = tmp_path / "demand_daily_v1.csv"
    rows = DATASET.read_bytes().split(b"\n")
    altered.write_bytes(b"\n".join(rows[:-2]))  # one observation short of the approved dataset

    refused = False
    try:
        build(tmp_path / "out", APPROVED_METADATA, altered)
    except Exception:  # the layer that refuses is not the point; that one does
        refused = True

    assert refused, "a dataset that is not the approved one was accepted"
    assert not (tmp_path / "out" / "model.pkl").exists()


def test_a_refusal_is_reported_as_a_non_zero_exit(tmp_path: Path) -> None:
    """`main` is what the Dockerfile RUN invokes; a refusal must fail the build."""
    from ml.pipelines.build_production_artifact import main

    code = main(
        [
            "--out",
            str(tmp_path / "out"),
            "--approved-metadata",
            str(tampered(tmp_path, artifact__canonical_model_digest="0" * 64)),
            "--dataset",
            str(DATASET),
        ]
    )

    assert code == 1


# --- the image's contents, as declared by the Dockerfile ---------------------------------------


def ml_import_closure() -> set[str]:
    """Every `ml/` file reachable from the two modules the serving boundary imports."""

    def dependencies(module: str) -> set[str]:
        path = REPOSITORY_ROOT / (module.replace(".", "/") + ".py")
        if not path.is_file():
            path = REPOSITORY_ROOT / module.replace(".", "/") / "__init__.py"
        found: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("ml"):
                found.add(node.module)
            elif isinstance(node, ast.Import):
                found.update(a.name for a in node.names if a.name.startswith("ml"))
        return found

    seen: set[str] = set()
    stack = ["ml.artifact", "ml.inference"]
    while stack:
        module = stack.pop()
        if module in seen:
            continue
        seen.add(module)
        stack.extend(dependencies(module) - seen)

    files = {"ml/__init__.py", "ml/pipelines/__init__.py"}
    for module in seen:
        path = REPOSITORY_ROOT / (module.replace(".", "/") + ".py")
        files.add(
            (path if path.is_file() else REPOSITORY_ROOT / module.replace(".", "/") / "__init__.py")
            .relative_to(REPOSITORY_ROOT)
            .as_posix()
        )
    return files


def instructions(text: str) -> str:
    """A Dockerfile with its comments removed and its continuations joined.

    Prose explaining why something is absent must not read as that something being present. Four
    tests here failed on exactly that -- the runtime stage's comment says "rather than
    `COPY ml ./ml`", and a substring search found the very instruction the comment rules out.
    """
    joined = re.sub(r"\\\s*\n\s*", " ", text)
    return "\n".join(
        line for line in joined.splitlines() if line.strip() and not line.lstrip().startswith("#")
    )


def requirement_lines(text: str) -> list[str]:
    """Declared packages only. The comments in requirements.txt discuss what is NOT installed."""
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def dockerfile_runtime_stage() -> str:
    return instructions(DOCKERFILE.split("FROM base AS runtime", 1)[1])


def copied_ml_files() -> set[str]:
    found: set[str] = set()
    for line in dockerfile_runtime_stage().splitlines():
        if not line.startswith("COPY ") or "--from=" in line:
            continue
        for token in line.split()[1:-1]:
            if token.startswith("ml/"):
                found.add(token)
    return found


def test_the_dockerfile_ships_exactly_the_import_closure() -> None:
    """Computed, not trusted. A module added to the closure without being copied would make the
    image fail on import; one copied without being needed is training code that should not ship."""
    assert copied_ml_files() == ml_import_closure()


def test_the_pipeline_entry_points_do_not_ship() -> None:
    """The commands that fit, evaluate and accept a model have no business in a serving image."""
    for entry in (
        "build_demand_dataset",
        "build_demand_artifact",
        "evaluate_demand_model",
        "validate_demand_model",
        "build_production_artifact",
    ):
        assert f"ml/pipelines/{entry}.py" not in dockerfile_runtime_stage(), entry


def test_no_dataset_is_copied_into_the_runtime_stage() -> None:
    """The point of the disposable builder stage, asserted on the COPY sources themselves."""
    sources = {
        token
        for line in dockerfile_runtime_stage().splitlines()
        if line.startswith("COPY ")
        for token in line.split()[1:-1]
        if not token.startswith("--")
    }

    for source in sources:
        assert not source.startswith("ml/data"), source
        assert not source.startswith("ml/notebooks"), source
        assert not source.startswith("ml/manifests/"), source
        assert not source.startswith("tests"), source
        assert source != "ml/requirements-ml.txt"


def test_the_whole_ml_directory_is_copied_only_into_the_disposable_stage() -> None:
    builder = instructions(
        DOCKERFILE.split("FROM base AS artifact-builder", 1)[1].split("FROM base AS runtime")[0]
    )

    assert "COPY ml ./ml" in builder
    assert "COPY ml ./ml" not in dockerfile_runtime_stage()


def test_the_artifact_comes_from_the_disposable_stage() -> None:
    stage = dockerfile_runtime_stage()

    assert "--from=artifact-builder /artifact/model.pkl /artifact/artifact.json" in stage
    assert "ml/models/demand_baseline_v1/" in stage


def test_the_build_runs_the_verifying_builder() -> None:
    assert "python -m ml.pipelines.build_production_artifact --out /artifact" in DOCKERFILE


def test_the_image_runs_as_a_non_root_user_created_after_every_copy() -> None:
    stage = dockerfile_runtime_stage()

    assert stage.index("COPY --from=artifact-builder") < stage.index("RUN useradd")
    assert "USER appuser" in stage


def test_the_context_keeps_every_pickle_out() -> None:
    """A developer's tree usually holds ml/models/demand_baseline_v1/model.pkl. It must never
    be swept into a build."""
    assert "**/*.pkl" in DOCKERIGNORE


def test_the_context_still_excludes_the_source_data_and_the_notebooks() -> None:
    for excluded in ("ml/data/raw", "ml/data/external", "ml/notebooks", "tests", "docs"):
        assert re.search(rf"^{re.escape(excluded)}$", DOCKERIGNORE, re.MULTILINE), excluded


# --- dependencies -------------------------------------------------------------------------------


def test_the_production_runtime_declares_scikit_learn_once_and_pinned() -> None:
    pins = re.findall(r"^scikit-learn==(\d+\.\d+\.\d+)$", REQUIREMENTS, re.MULTILINE)

    assert pins == ["1.9.1"]


def test_the_two_declarations_of_scikit_learn_agree() -> None:
    """The model is served by the library version it was fitted with, not a nearby one."""
    offline = (REPOSITORY_ROOT / "ml" / "requirements-ml.txt").read_text(encoding="utf-8")
    offline_pins = re.findall(r"^scikit-learn==(\d+\.\d+\.\d+)$", offline, re.MULTILINE)
    production_pins = re.findall(r"^scikit-learn==(\d+\.\d+\.\d+)$", REQUIREMENTS, re.MULTILINE)

    assert offline_pins == production_pins == ["1.9.1"]


@pytest.mark.parametrize("library", ["pandas", "torch", "tensorflow", "xgboost", "lightgbm"])
def test_no_further_ml_library_reached_production(library: str) -> None:
    declared = [line.lower() for line in requirement_lines(REQUIREMENTS)]

    assert not [line for line in declared if line.startswith(library)], declared


def test_numpy_and_scipy_are_not_declared_separately() -> None:
    """They arrive as scikit-learn's own requirements; declaring them would assert a
    combination scikit-learn has not been tested against."""
    declared = requirement_lines(REQUIREMENTS)

    for transitive in ("numpy", "scipy", "joblib", "threadpoolctl"):
        assert not [line for line in declared if line.lower().startswith(transitive)], declared


def test_the_development_requirements_are_still_not_installed_by_the_image() -> None:
    """pytest, ruff, mypy and httpx are not runtime inputs and never have been."""
    assert "requirements-dev" not in instructions(DOCKERFILE)


# --- no model is fetched from anywhere -----------------------------------------------------------


@pytest.mark.parametrize(
    "fetcher", ["urllib", "requests", "httpx", "aiohttp", "socket", "boto3", "curl", "wget"]
)
def test_nothing_on_the_artifact_path_can_fetch_a_model(fetcher: str) -> None:
    """The artifact is regenerated from a committed dataset. There is no download, at build time
    or at runtime, and no URL for one to come from."""
    sources = [
        REPOSITORY_ROOT / "ml" / "pipelines" / "build_production_artifact.py",
        REPOSITORY_ROOT / "backend" / "app" / "ml" / "artifact_store.py",
        REPOSITORY_ROOT / "backend" / "app" / "ml" / "serving.py",
        REPOSITORY_ROOT / "ml" / "artifact.py",
    ]
    for source in sources:
        text = source.read_text(encoding="utf-8")
        assert not re.search(rf"^\s*(?:import|from)\s+{re.escape(fetcher)}\b", text, re.M), source


def test_the_dockerfile_downloads_nothing_but_its_pinned_dependencies() -> None:
    for fetcher in ("curl", "wget", "ADD http", "git clone"):
        assert fetcher not in DOCKERFILE, fetcher


def test_no_url_appears_on_the_artifact_path() -> None:
    builder = (REPOSITORY_ROOT / "ml" / "pipelines" / "build_production_artifact.py").read_text(
        encoding="utf-8"
    )

    assert "http://" not in builder
    assert "https://" not in builder
