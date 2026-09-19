"""The first fitted artifact for ``demand_baseline_v1``, and the rules that keep it honest.

Stage 6.3 fitted a model 54 times and kept none of them. Stage 6.5 fits it **once**, on the
dataset's declared training partition, and keeps that one -- so that a later stage has something
to load rather than something to re-derive.

## What this is not

It is not a deployment artifact. It is not loaded by the API, it is not reachable over HTTP, and
nothing in ``backend/`` imports this module -- a test asserts it. The metadata says so in
machine-readable form: ``production_ready``, ``production_accuracy_established``,
``cross_hotel_generalisation_established`` and ``serving_enabled`` are all ``false``, and tests
require them to stay that way.

## Format: the standard library, deliberately

``pickle`` at protocol 5, not ``joblib``. joblib is present in the environment as one of
scikit-learn's own requirements, but *declaring* it would add a direct dependency for a
capability the standard library already covers: its advantage is memory-mapped NumPy arrays for
artifacts far larger than this one, which is 695 KB. The repository's rule is that direct
dependencies are pinned and transitive ones are not used directly, and this stage keeps it.

**The payload is not committed.** ``.gitignore`` has said since Stage 1 that *"weights are large
binaries and are never committed"*, and a pickle is arbitrary code on load -- distributing one
through `git clone` is exactly the thing §"Trust boundary" below exists to prevent. What is
committed is ``artifact.json``: the checksums, the versions and the reproducibility digest that
make a regenerated payload verifiable rather than merely present.

## Two checksums, because they answer different questions

``artifact_sha256`` is the digest of the serialised bytes. On this build, refitting from scratch
reproduces them **byte for byte** -- measured, not assumed -- but pickle output is a property of
the interpreter, the scikit-learn build and the NumPy build, and no claim is made that it holds
across toolchains.

``canonical_model_digest`` is the reproducibility digest: a hash over the feature columns, the
estimator configuration, the training extent and the model's predictions on a fixed synthetic
probe grid, each formatted to six decimal places. It is a fingerprint of *what the model
computes* rather than of how it was written down, so it survives a serialisation change and
would not survive a change to the model.

## Trust boundary

A pickle executes arbitrary code when it is loaded. :func:`load_artifact` therefore reads and
validates ``artifact.json`` -- schema, model version, dataset checksum, feature version, feature
columns, horizon, and the payload's own sha256 -- **before** unpickling anything. A payload whose
digest does not match its metadata is refused untouched. The repository never distributes a
payload, the API never loads one, and there is no code path anywhere that takes an artifact path
from a request.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import pickle
import platform
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy
import scipy
import sklearn

from ml.evaluation import RollingOriginPolicy
from ml.loading import ProcessedDataset, ProcessedRow
from ml.manifests import serialise
from ml.models import (
    MODEL_NAME,
    MODEL_VERSION,
    EstimatorConfig,
    LearnedModel,
    design_matrix,
    select_model_features,
)
from ml.pipelines.offline_demand import sha256_hex
from ml.policy import ACCEPTANCE_POLICY
from ml.registry import (
    ROLLING_ORIGIN_PROTOCOL_VERSION,
    STATUS_OFFLINE_CANDIDATE,
    configuration_checksum,
    protocol_checksum,
)
from ml.validation import assert_canonical_feature_order, assert_model_version

#: Bump when the shape of ``artifact.json`` changes.
ARTIFACT_SCHEMA_VERSION = "artifact_v1"

ARTIFACT_FORMAT = "pickle"
PICKLE_PROTOCOL = 5

#: The partition the artifact is fitted on. Named rather than assumed: validation and test rows
#: must never reach ``fit``, and :func:`training_rows` refuses anything else.
TRAINING_PARTITION = "train"
HELD_OUT_PARTITIONS: tuple[str, ...] = ("validation", "test")

DEFAULT_ARTIFACT_DIRECTORY = Path(__file__).resolve().parent / "models" / MODEL_VERSION
DEFAULT_MODEL_PAYLOAD = DEFAULT_ARTIFACT_DIRECTORY / "model.pkl"
DEFAULT_ARTIFACT_METADATA = DEFAULT_ARTIFACT_DIRECTORY / "artifact.json"

#: Rows in the synthetic probe grid used for the reproducibility digest.
PROBE_ROWS = 64

#: Decimal places the probe predictions are formatted to before hashing. Coarse enough that a
#: last-bit floating-point difference between toolchains cannot move the digest, fine enough
#: that any real change to the model does. Room-night predictions here are of order 100, so six
#: decimals is roughly five orders of magnitude finer than the differences a compiler can cause.
PROBE_DIGITS = 6


class ArtifactError(Exception):
    """The artifact, or the thing it was asked to be built from, is not what it claims."""


# --- training ------------------------------------------------------------------------------------


#: The order the rolling-origin protocol presents rows to the estimator in. It is **not** the
#: dataset file's order: Stage 6.1 writes rows sorted by ``(target_date, str(hotel_public_id))``,
#: while ``ml.evaluation.evaluate`` re-sorts by ``(target_date, hotel_key)``. Those disagree
#: within a date -- ``resort_hotel`` is ``c31c4e41...`` and ``city_hotel`` ``c7fb00b8...``, so the
#: public-id order is the reverse of the key order -- and the artifact adopts the protocol's,
#: because the point of the artifact is to be the model the protocol measured.
TRAINING_ROW_ORDER = ("target_date", "hotel_key")


def training_rows(dataset: ProcessedDataset) -> tuple[ProcessedRow, ...]:
    """The declared training partition, in the protocol's row order, and nothing else.

    Not "rows before some date" -- the partition column Stage 6.2 wrote. Deriving the boundary
    here would create a second definition of where training stops, and two definitions of one
    boundary is how a validation row ends up in a fit.

    The sort matters and is not cosmetic. ``HistGradientBoostingRegressor`` turns out to be
    row-order invariant for this configuration -- measured, and asserted by a test -- so the
    fitted model is the same either way. But relying on that would make "the artifact is the
    Stage 6.3 fold-11 model" an accident of the estimator's internals rather than a property of
    the inputs. Sorting here makes the two estimator input matrices identical **as sequences**,
    which is the claim the equivalence test actually needs.
    """
    rows = tuple(
        sorted(
            (row for row in dataset.rows if row.partition == TRAINING_PARTITION),
            key=lambda row: (row.target_date, row.hotel_key),
        )
    )
    if not rows:
        raise ArtifactError(f"the dataset carries no rows in the {TRAINING_PARTITION!r} partition")
    return rows


def assert_no_held_out_row(rows: Sequence[ProcessedRow], dataset: ProcessedDataset) -> None:
    """Refuse a training set that reaches into validation or test.

    Two independent checks, because they fail differently: a mislabelled row is caught by the
    partition check, and a correctly labelled row from the wrong side of the boundary is caught
    by the date check.
    """
    wrong_partition = sorted({row.partition for row in rows if row.partition != TRAINING_PARTITION})
    if wrong_partition:
        raise ArtifactError(
            f"training rows carry partition(s) {wrong_partition}; only "
            f"{TRAINING_PARTITION!r} may be fitted"
        )
    held_out = [row.target_date for row in dataset.rows if row.partition in HELD_OUT_PARTITIONS]
    if not held_out:
        return
    latest_train = max(row.target_date for row in rows)
    earliest_held_out = min(held_out)
    if latest_train >= earliest_held_out:
        raise ArtifactError(
            f"training reaches {latest_train} while held-out data starts {earliest_held_out}"
        )


def assert_all_finite(rows: Sequence[ProcessedRow], feature_columns: Sequence[str]) -> None:
    """Refuse NaN or infinity in any feature that will be fitted.

    ``None`` is a different thing and is handled elsewhere: a missing value means the row is
    held out of the fit, while a NaN means the dataset is wrong.
    """
    for row in rows:
        for name in feature_columns:
            value = row.features.get(name)
            if value is None:
                continue
            if not math.isfinite(value):
                raise ArtifactError(
                    f"non-finite value {value!r} for {name} on {row.target_date} ({row.hotel_key})"
                )


@dataclass(frozen=True, slots=True)
class TrainedArtifact:
    """A fitted model plus every fact needed to say what produced it."""

    model: LearnedModel
    feature_columns: tuple[str, ...]
    horizon_days: int
    training_start: dt.date
    training_end: dt.date
    partition_rows: int
    fitted_rows: int
    held_out_for_missing_features: int
    training_hotels: tuple[str, ...]
    training_dates: int
    dataset_sha256: str
    dataset_version: str
    feature_version: str

    @property
    def estimator(self) -> Any:
        return self.model.estimator


def train_artifact(
    dataset: ProcessedDataset,
    *,
    dataset_version: str,
    feature_version: str,
    horizon_days: int = RollingOriginPolicy().horizon_days,
    config: EstimatorConfig | None = None,
    model_version: str = MODEL_VERSION,
) -> TrainedArtifact:
    """Fit the Stage 6.3 estimator once, on the declared training partition.

    Every refusal in the module docstring happens here, in order, before ``fit`` is reached.
    The estimator configuration is Stage 6.3's default and is **not** a tunable of this stage:
    passing a different one is possible for tests, and the pipeline never does.
    """
    assert_model_version(model_version)
    assert_canonical_feature_order(dataset.feature_names)
    selection = select_model_features(dataset.feature_names, horizon_days=horizon_days)

    rows = training_rows(dataset)
    assert_no_held_out_row(rows, dataset)
    assert_all_finite(rows, selection.selected)

    matrix = design_matrix(rows, selection.selected)
    if not matrix.features:
        raise ArtifactError(
            f"no row in the {TRAINING_PARTITION!r} partition has all "
            f"{len(selection.selected)} selected feature(s) present"
        )

    fitted = [rows[index] for index in matrix.used]
    model = LearnedModel.fit(rows, selection.selected, config or EstimatorConfig())
    return TrainedArtifact(
        model=model,
        feature_columns=selection.selected,
        horizon_days=horizon_days,
        training_start=min(row.target_date for row in fitted),
        training_end=max(row.target_date for row in fitted),
        partition_rows=len(rows),
        fitted_rows=len(fitted),
        held_out_for_missing_features=len(matrix.skipped),
        training_hotels=tuple(sorted({row.hotel_key for row in fitted})),
        training_dates=len({row.target_date for row in fitted}),
        dataset_sha256=dataset.sha256,
        dataset_version=dataset_version,
        feature_version=feature_version,
    )


# --- the reproducibility digest ---------------------------------------------------------------


def probe_matrix(rows: int = PROBE_ROWS) -> tuple[tuple[float, ...], ...]:
    """A fixed synthetic feature grid, in the canonical column order.

    Synthetic and not drawn from the dataset, on purpose: the digest is a fingerprint of the
    fitted model, and a fingerprint computed from held-out rows would blur the line between
    identifying a model and evaluating one. Every value is an integer, so the *input* to the
    digest carries no floating-point noise of its own.
    """
    if rows < 1:
        raise ArtifactError(f"probe grid needs at least one row, got {rows}")
    grid: list[tuple[float, ...]] = []
    for index in range(rows):
        day_of_week = index % 7
        grid.append(
            (
                float(day_of_week),
                float(index % 28 + 1),
                float(index % 12 + 1),
                float(index % 52 + 1),
                float((index * 5) % 365 + 1),
                float(1 if day_of_week >= 5 else 0),
                float(100 + (index * 3) % 90),
                float(100 + (index * 5) % 90),
                float(100 + (index * 7) % 90),
            )
        )
    return tuple(grid)


def probe_predictions(estimator: Any, rows: int = PROBE_ROWS) -> tuple[str, ...]:
    """The model's answers on the probe grid, formatted to a fixed number of decimals."""
    raw = estimator.predict([list(values) for values in probe_matrix(rows)])
    return tuple(f"{float(value):.{PROBE_DIGITS}f}" for value in raw)


def canonical_model_digest(trained: TrainedArtifact) -> str:
    """A digest of what the model computes, not of how it was serialised.

    Survives a change of serialisation format; does not survive a change to the features, the
    configuration, the training extent or a single prediction beyond the sixth decimal.
    """
    payload = {
        "canonical_digest_version": ARTIFACT_SCHEMA_VERSION,
        "feature_columns": list(trained.feature_columns),
        "estimator_configuration": trained.model.config.as_dict(),
        "forecast_horizon_days": trained.horizon_days,
        "training_row_count": trained.fitted_rows,
        "training_start_date": trained.training_start.isoformat(),
        "training_end_date": trained.training_end.isoformat(),
        "probe_rows": PROBE_ROWS,
        "probe_digits": PROBE_DIGITS,
        "probe_predictions": list(probe_predictions(trained.estimator)),
    }
    return sha256_hex(serialise(payload))


# --- serialisation ---------------------------------------------------------------------------


def serialise_model(estimator: Any) -> bytes:
    return pickle.dumps(estimator, protocol=PICKLE_PROTOCOL)


def deserialise_model(payload: bytes) -> Any:
    """Unpickle. **Only ever called after the payload's digest has been checked.**

    There is no safe way to unpickle an untrusted payload, which is why validation happens
    first and why this function is not exported as a convenience.
    """
    try:
        # The payload's sha256 was compared against its metadata before this call; see the
        # module docstring. Unpickling anything that has not passed that check is the one
        # thing this module exists to prevent.
        return pickle.loads(payload)
    except Exception as exc:  # pragma: no cover - corrupt input is exercised via load_artifact
        raise ArtifactError(f"the artifact payload could not be deserialised: {exc}") from exc


# --- metadata ---------------------------------------------------------------------------------


def build_artifact_metadata(
    trained: TrainedArtifact,
    *,
    artifact_sha256: str,
    artifact_bytes: int,
    artifact_filename: str,
    dataset_path: str,
    validation_sha256: str,
    registry_sha256: str,
    created_at: dt.datetime | None = None,
) -> dict[str, object]:
    """``artifact.json``. Everything outside ``generation`` is a function of the inputs."""
    configuration = trained.model.config.as_dict()
    protocol_settings = RollingOriginPolicy().as_dict()
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "model": {
            "model_version": MODEL_VERSION,
            "model_name": MODEL_NAME,
            "status": STATUS_OFFLINE_CANDIDATE,
            "forecast_horizon_days": trained.horizon_days,
            "estimator_configuration": configuration,
            "estimator_configuration_sha256": configuration_checksum(configuration),
        },
        "artifact": {
            "format": ARTIFACT_FORMAT,
            "format_note": (
                "Python pickle, protocol 5, written by the standard library. joblib is present "
                "only as a scikit-learn requirement and is deliberately not used directly."
            ),
            "filename": artifact_filename,
            "sha256": artifact_sha256,
            "bytes": artifact_bytes,
            "committed": False,
            "committed_note": (
                "The payload is generated, not committed: .gitignore has excluded model weights "
                "since Stage 1, and a pickle is arbitrary code on load. This metadata file is "
                "what is committed, and it is what makes a regenerated payload verifiable."
            ),
            "canonical_model_digest": canonical_model_digest(trained),
            "canonical_digest_note": (
                "sha256 over the feature columns, estimator configuration, training extent and "
                "the model's predictions on a fixed synthetic probe grid, each formatted to "
                f"{PROBE_DIGITS} decimal places. A fingerprint of what the model computes, not "
                "of how it was serialised."
            ),
            "byte_reproducibility": (
                "Refitting from scratch on this build reproduces the payload byte for byte "
                "(measured). No claim is made that this holds across interpreter, scikit-learn "
                "or NumPy builds; the canonical digest is the cross-build claim."
            ),
            "probe_rows": PROBE_ROWS,
            "probe_digits": PROBE_DIGITS,
            "probe_predictions": list(probe_predictions(trained.estimator)),
        },
        "dataset": {
            "dataset_version": trained.dataset_version,
            "feature_version": trained.feature_version,
            "dataset_sha256": trained.dataset_sha256,
            "path": dataset_path,
            "feature_columns": list(trained.feature_columns),
        },
        "training": {
            "partition": TRAINING_PARTITION,
            "partition_rows": trained.partition_rows,
            "training_row_count": trained.fitted_rows,
            "held_out_for_missing_features": trained.held_out_for_missing_features,
            "training_start_date": trained.training_start.isoformat(),
            "training_end_date": trained.training_end.isoformat(),
            "training_hotel_count": len(trained.training_hotels),
            "training_hotels": list(trained.training_hotels),
            "training_date_count": trained.training_dates,
            "held_out_partitions": list(HELD_OUT_PARTITIONS),
            "row_order": list(TRAINING_ROW_ORDER),
            "row_order_note": (
                "The rolling-origin protocol's order, not the dataset file's. The two "
                "disagree within a date, and the artifact adopts the protocol's so that "
                "its estimator input matrix is identical to fold 11's as a sequence, not "
                "merely as a set."
            ),
            "accounting_note": (
                "partition_rows counts the declared training partition; training_row_count "
                "counts the rows that actually reached estimator.fit() after rows with a "
                "missing selected feature were held out. They are different numbers and are "
                "reported separately."
            ),
            "note": (
                "Fitted once on the dataset's declared training partition. Validation and test "
                "rows never reach fit, and two independent checks enforce it."
            ),
        },
        "protocol": {
            "protocol_version": ROLLING_ORIGIN_PROTOCOL_VERSION,
            "protocol_sha256": protocol_checksum(protocol_settings),
            "acceptance_policy_version": ACCEPTANCE_POLICY.version,
            "acceptance_policy_sha256": ACCEPTANCE_POLICY.checksum(),
            "validation_record_sha256": validation_sha256,
            "registry_record_sha256_at_build": registry_sha256,
            "note": (
                "The Stage 6.4 validation record remains authoritative. This artifact does not "
                "re-run acceptance and does not restate its result."
            ),
            "registry_reference_note": (
                "The registry checksum is the state the artifact was built AGAINST, before the "
                "artifact block was attached to it. The two records reference each other, and "
                "only one direction can be a checksum of the other: this one. The validation "
                "record is never amended, so its checksum is unconditional."
            ),
        },
        "claims": {
            "production_ready": False,
            "production_accuracy_established": False,
            "cross_hotel_generalisation_established": False,
            "serving_enabled": False,
            "note": (
                "Offline research artifact. Not loaded by the API, not reachable over HTTP, and "
                "no code path takes an artifact location from a request."
            ),
        },
        "generation": {
            "note": (
                "Wall-clock and toolchain metadata, outside the content checksum: rebuilding "
                "the artifact must not look like a different artifact."
            ),
            "created_at": (created_at or dt.datetime.now(dt.UTC)).isoformat(),
            "created_by": "ml/pipelines/build_demand_artifact.py",
            "environment": {
                "python": sys.version.split()[0],
                "platform": platform.system(),
                "scikit_learn": sklearn.__version__,
                "numpy": numpy.__version__,
                "scipy": scipy.__version__,
                "pickle_protocol": PICKLE_PROTOCOL,
            },
        },
    }


# --- loading, with the trust boundary in front of it ---------------------------------------------


@dataclass(frozen=True, slots=True)
class LoadedArtifact:
    """A verified artifact: its metadata, and the estimator it was allowed to unpickle."""

    metadata: Mapping[str, object]
    estimator: Any
    model_version: str
    feature_columns: tuple[str, ...]
    forecast_horizon_days: int
    feature_version: str
    dataset_sha256: str
    canonical_model_digest: str


def _block(metadata: Mapping[str, object], name: str) -> Mapping[str, object]:
    block = metadata.get(name)
    if not isinstance(block, dict):
        raise ArtifactError(f"the artifact metadata has no {name!r} block")
    return block


def load_artifact_metadata(path: Path = DEFAULT_ARTIFACT_METADATA) -> dict[str, object]:
    if not path.is_file():
        raise ArtifactError(
            f"{path} does not exist. Build it with `python -m ml.pipelines.build_demand_artifact`."
        )
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ArtifactError(f"{path} is not a JSON object")
    return loaded


def load_artifact(
    payload_path: Path = DEFAULT_MODEL_PAYLOAD,
    metadata_path: Path = DEFAULT_ARTIFACT_METADATA,
    *,
    expected_model_version: str = MODEL_VERSION,
    expected_dataset_sha256: str | None = None,
    expected_feature_version: str = "v1",
) -> LoadedArtifact:
    """Validate first, unpickle second. The order is the security property.

    Nine checks run against ``artifact.json`` and the payload's digest before a single byte is
    deserialised, because unpickling is arbitrary code execution and a digest compared
    afterwards would be a digest compared too late.
    """
    metadata = load_artifact_metadata(metadata_path)

    schema = metadata.get("schema_version")
    if schema != ARTIFACT_SCHEMA_VERSION:
        raise ArtifactError(f"artifact schema {schema!r} is not {ARTIFACT_SCHEMA_VERSION!r}")

    model = _block(metadata, "model")
    artifact = _block(metadata, "artifact")
    dataset = _block(metadata, "dataset")
    claims = _block(metadata, "claims")

    if model.get("model_version") != expected_model_version:
        raise ArtifactError(
            f"artifact declares model_version {model.get('model_version')!r}, expected "
            f"{expected_model_version!r}"
        )
    if dataset.get("feature_version") != expected_feature_version:
        raise ArtifactError(
            f"artifact declares feature_version {dataset.get('feature_version')!r}, expected "
            f"{expected_feature_version!r}"
        )
    if expected_dataset_sha256 is not None and dataset.get("dataset_sha256") != (
        expected_dataset_sha256
    ):
        raise ArtifactError(
            f"artifact was built from dataset {dataset.get('dataset_sha256')!r}, expected "
            f"{expected_dataset_sha256!r}"
        )
    if artifact.get("format") != ARTIFACT_FORMAT:
        raise ArtifactError(f"artifact format {artifact.get('format')!r} is not supported")
    if claims.get("serving_enabled") is not False:
        raise ArtifactError("an artifact declaring serving_enabled is refused by this stage")

    columns = dataset.get("feature_columns")
    if not isinstance(columns, list) or not columns:
        raise ArtifactError("the artifact metadata declares no feature columns")
    horizon = model.get("forecast_horizon_days")
    if not isinstance(horizon, int) or horizon < 1:
        raise ArtifactError(f"the artifact declares an unusable horizon {horizon!r}")

    if not payload_path.is_file():
        raise ArtifactError(
            f"{payload_path} does not exist. The payload is generated rather than committed; "
            "build it with `python -m ml.pipelines.build_demand_artifact`."
        )
    payload = payload_path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != artifact.get("sha256"):
        raise ArtifactError(
            f"{payload_path} hashes to {digest}, but its metadata claims "
            f"{artifact.get('sha256')}; the payload was not deserialised"
        )

    estimator = deserialise_model(payload)
    if not hasattr(estimator, "predict"):
        raise ArtifactError("the deserialised object does not expose predict()")

    return LoadedArtifact(
        metadata=metadata,
        estimator=estimator,
        model_version=str(model["model_version"]),
        feature_columns=tuple(str(name) for name in columns),
        forecast_horizon_days=horizon,
        feature_version=str(dataset["feature_version"]),
        dataset_sha256=str(dataset["dataset_sha256"]),
        canonical_model_digest=str(artifact.get("canonical_model_digest", "")),
    )
