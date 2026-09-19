"""The artifact loading boundary: the one place in the application that touches ``ml/``.

This is the trust boundary between the running API and an offline research artifact. It exists
so that "the server loads a model" is a single, auditable, forty-line decision rather than
something spread across a service and a router.

## The rule this module exists to enforce

**Validate, then deserialise. Never the other way round.** A pickle executes arbitrary code
when it is read, so a digest compared afterwards is a digest compared too late.
:func:`ml.artifact.load_artifact` already checks the metadata and the payload's SHA-256 before
unpickling anything; this module adds the checks that only the *server* can make -- that the
artifact is the one model this deployment approved -- and then one behavioural check that binds
the unpickled object to the metadata it arrived with.

## Why the import of ``ml`` is deferred

``ml/`` is not part of the API image. ``.dockerignore`` excludes it from the build context and
``backend/requirements.txt`` names no ML package, both deliberately and both unchanged by this
stage. So in a shipped container this import fails, and it must fail as a *served 503* rather
than as an application that cannot start. Importing inside the loader is what makes the ML
runtime an optional capability rather than a hard dependency of the whole API.

The practical consequence is stated plainly in ``docs/ml-serving.md`` and is not hidden: the
demand endpoint answers 503 wherever the ML runtime or the artifact is absent, which today
includes the shipped image. Making it answer anything else is a deployment decision, and a
deployment decision is not a thing this stage is authorised to take.

## Caching, and the concurrency assumptions behind it

The artifact is loaded **at most once per process** and then reused. Three properties make that
safe, and each is asserted by a test rather than assumed:

* **Loading is serialised.** A module-level lock guards the load, so two threads arriving
  together produce one load and one object, not two.
* **The loaded object is never mutated.** :class:`ml.artifact.LoadedArtifact` is a frozen
  dataclass, nothing here writes to it or to the estimator it carries, and inference calls
  ``predict`` and nothing else. ``HistGradientBoostingRegressor.predict`` reads the fitted
  trees; it fits nothing and stores nothing.
* **Failure is cached too.** An unavailable or rejected artifact is remembered, so a missing
  file cannot turn into a filesystem probe on every request, and a rejected artifact cannot be
  re-unpickled per request.

The cache is **process-wide rather than per application instance**, and that is deliberate.
``create_app`` runs hundreds of times in the test suite, and an ``app.state`` cache would make
the number of loads a function of how many applications a process built. Logging is placed in
the lifespan for exactly this reason; the same argument applies here. :func:`reset` exists so a
test can start from a known state, and nothing reachable from a request calls it.

## What a request can and cannot influence

Nothing here takes a path, a version, a filename or a format from a caller. :func:`configure`
sets the location, it is module-level rather than request-scoped, and a static test asserts
that no router, schema or service calls it.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.ml.serving import (
    APPROVED_MODEL,
    EXPECTED_CLAIMS,
    ApprovedModel,
    ArtifactRejectedError,
    ArtifactUnavailableError,
    InferenceFailedError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only; the runtime import is deferred below.
    from ml.artifact import LoadedArtifact

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ArtifactLocation:
    """Where the approved artifact's two files are.

    Two paths rather than a directory: the metadata is read and validated first and the payload
    only afterwards, and keeping them separate makes that order impossible to lose.
    """

    payload: Path
    metadata: Path


@dataclass(frozen=True, slots=True)
class ServedModel:
    """A verified artifact, ready to be asked for predictions and for nothing else.

    It carries the identity it was checked against, so a response can state which model
    produced a number without consulting this module's constants a second time.
    """

    artifact: LoadedArtifact
    model: ApprovedModel

    @property
    def model_version(self) -> str:
        return self.model.model_version

    @property
    def feature_columns(self) -> tuple[str, ...]:
        return self.model.feature_columns


# --- module state -----------------------------------------------------------------------------
#
# Guarded by _LOCK. _RESULT holds whichever outcome the single load produced: the model, or the
# refusal to be re-raised. _LOADS counts loads for the regression test that says the artifact is
# not read per request.

_LOCK = threading.Lock()
_RESULT: ServedModel | ServingFailure | None = None
_LOCATION: ArtifactLocation | None = None
_LOADS = 0


@dataclass(frozen=True, slots=True)
class ServingFailure:
    """A remembered refusal, so a failed load is not retried on every request."""

    error: ArtifactUnavailableError | ArtifactRejectedError


def configure(location: ArtifactLocation | None) -> None:
    """Point the store at an artifact pair, or back at the default, and drop the cache.

    Module-level and never request-reachable. ``None`` restores the default location, which is
    resolved from the ``ml`` package itself rather than from configuration -- there is no
    setting, no environment variable and no request parameter that names an artifact.
    """
    global _LOCATION
    with _LOCK:
        _LOCATION = location
        _drop()


def reset() -> None:
    """Forget the loaded artifact and the load count. For tests."""
    with _LOCK:
        _drop()


def load_count() -> int:
    """How many times an artifact has actually been read and deserialised in this process."""
    return _LOADS


def _drop() -> None:
    """Clear the cached outcome. The caller holds the lock."""
    global _RESULT, _LOADS
    _RESULT = None
    _LOADS = 0


def approved_model() -> ServedModel:
    """The verified artifact, loading it once if this is the first ask.

    Raises :class:`~app.ml.serving.ArtifactUnavailableError` or
    :class:`~app.ml.serving.ArtifactRejectedError`. Both are remembered: a second request gets the
    same refusal without touching the filesystem again.
    """
    global _RESULT, _LOADS
    cached = _RESULT
    if cached is not None:
        return _unwrap(cached)

    with _LOCK:
        # Re-read under the lock: another thread may have loaded it while this one waited.
        if _RESULT is not None:
            return _unwrap(_RESULT)
        try:
            loaded = _load(_LOCATION)
        except (ArtifactUnavailableError, ArtifactRejectedError) as error:
            _RESULT = ServingFailure(error)
            raise
        _LOADS += 1
        _RESULT = loaded
        return loaded


def _unwrap(result: ServedModel | ServingFailure) -> ServedModel:
    if isinstance(result, ServingFailure):
        raise result.error
    return result


# --- the load itself ----------------------------------------------------------------------------


def _load(location: ArtifactLocation | None) -> ServedModel:
    """Import the ML runtime, read the artifact, and refuse anything that is not the approved one.

    Every failure below becomes one of two exceptions carrying a message written for an
    operator's log. None of those messages reaches a client: the service maps both onto a single
    fixed sentence, because "which of the sixteen checks failed" is information about the
    server's internals rather than about the request.
    """
    try:
        from ml.artifact import (
            DEFAULT_ARTIFACT_METADATA,
            DEFAULT_MODEL_PAYLOAD,
            ArtifactError,
            load_artifact,
            probe_predictions,
        )
    except ImportError as error:
        logger.warning("Demand model serving is unavailable: the ML runtime is not installed")
        raise ArtifactUnavailableError(
            "the offline ml package is not importable in this runtime"
        ) from error

    where = location or ArtifactLocation(
        payload=DEFAULT_MODEL_PAYLOAD, metadata=DEFAULT_ARTIFACT_METADATA
    )
    for name, path in (("metadata", where.metadata), ("payload", where.payload)):
        if not path.is_file():
            logger.warning("Demand model serving is unavailable: the artifact %s is absent", name)
            raise ArtifactUnavailableError(f"the artifact {name} is not present")

    model = APPROVED_MODEL
    try:
        # Checks the schema, the model version, the feature version, the dataset checksum, the
        # format and the payload's own SHA-256 -- and does all of it BEFORE unpickling.
        artifact = load_artifact(
            where.payload,
            where.metadata,
            expected_model_version=model.model_version,
            expected_dataset_sha256=model.dataset_sha256,
            expected_feature_version=model.feature_version,
        )
    except ArtifactError as error:
        logger.error("Demand model serving refused an artifact: %s", error)
        raise ArtifactRejectedError(str(error)) from None

    _verify(artifact, model, probe_predictions)
    logger.info(
        "Demand model serving loaded %s (feature_version=%s, horizon=%sd)",
        model.model_version,
        model.feature_version,
        model.forecast_horizon_days,
    )
    return ServedModel(artifact=artifact, model=model)


def _verify(artifact: Any, model: ApprovedModel, probe: Any) -> None:
    """The checks the server makes on its own behalf, after the offline loader's.

    The last of them is the one worth reading. Everything above it compares a declaration
    against a declaration -- the metadata says v1, the approval says v1. The probe comparison
    asks the *unpickled estimator* what it computes on a fixed synthetic grid and requires the
    answer to be the one recorded when the artifact was built. The probe predictions are an
    input to the canonical digest, so agreeing on them is what ties the digest this server
    pinned to the object it actually holds, rather than to the file's description of itself.
    """
    metadata = artifact.metadata
    block = _block(metadata, "model")
    dataset = _block(metadata, "dataset")
    artifact_block = _block(metadata, "artifact")
    claims = _block(metadata, "claims")

    _require(metadata.get("schema_version") == model.schema_version, "artifact schema version")
    _require(block.get("model_name") == model.model_name, "model name")
    _require(artifact_block.get("format") == model.artifact_format, "artifact format")
    _require(dataset.get("dataset_version") == model.dataset_version, "dataset version")
    _require(artifact.forecast_horizon_days == model.forecast_horizon_days, "forecast horizon")
    _require(artifact.feature_columns == model.feature_columns, "feature columns")
    _require(
        artifact.canonical_model_digest == model.canonical_model_digest,
        "canonical model digest",
    )
    _require(
        {key: claims.get(key) for key in EXPECTED_CLAIMS} == dict(EXPECTED_CLAIMS),
        "artifact claims",
    )

    declared = artifact_block.get("probe_predictions")
    _require(isinstance(declared, list) and bool(declared), "probe predictions")
    assert isinstance(declared, list)  # narrowing for the type checker; _require decides
    try:
        computed = list(probe(artifact.estimator))
    except Exception as error:
        raise ArtifactRejectedError(
            f"the deserialised estimator could not be probed ({type(error).__name__})"
        ) from None
    _require(computed == list(declared), "probe predictions")


def _block(metadata: Mapping[str, object], name: str) -> Mapping[str, object]:
    block = metadata.get(name)
    if not isinstance(block, dict):
        raise ArtifactRejectedError(f"the artifact metadata has no {name!r} block")
    return block


def _require(condition: bool, what: str) -> None:
    if not condition:
        raise ArtifactRejectedError(f"the artifact does not match the approved model: {what}")


# --- inference ------------------------------------------------------------------------------------


def predict_room_nights(
    served: ServedModel,
    *,
    hotel_public_id: uuid.UUID,
    target_date: dt.date,
    features: Mapping[str, float],
) -> float:
    """One prediction, through the Stage 6.5 inference contract and no other route.

    :func:`ml.inference.predict_demand` is called rather than reimplemented or wrapped in
    something more forgiving. It is the module that refuses a missing column, an extra column,
    a permuted column list, a non-finite value and a horizon that does not match the artifact --
    and every one of those refusals is a prediction that would otherwise have been produced from
    inputs the model was not fitted on.

    Nothing here calls ``fit``, ``fit_predict`` or ``partial_fit``; a test monkey-patches all
    three to raise and requires a request to succeed anyway.
    """
    from ml.inference import (
        FeatureVector,
        InferenceError,
        predict_demand,
    )

    row = FeatureVector(
        hotel_public_id=hotel_public_id,
        target_date=target_date,
        forecast_horizon_days=served.model.forecast_horizon_days,
        features=dict(features),
    )
    try:
        predictions: Sequence[Any] = predict_demand(
            served.artifact,
            (row,),
            expected_model_version=served.model.model_version,
            expected_feature_version=served.model.feature_version,
        )
    except InferenceError as error:
        logger.error("Demand inference refused a request: %s", error)
        raise InferenceFailedError("the inference contract refused the assembled row") from None
    except Exception as error:
        logger.error("Demand inference failed (%s)", type(error).__name__)
        raise InferenceFailedError("the estimator did not produce a prediction") from None

    if len(predictions) != 1:
        raise InferenceFailedError(f"expected one prediction, got {len(predictions)}")
    return float(predictions[0].prediction)


__all__ = [
    "ArtifactLocation",
    "ServedModel",
    "approved_model",
    "configure",
    "load_count",
    "predict_room_nights",
    "reset",
]
