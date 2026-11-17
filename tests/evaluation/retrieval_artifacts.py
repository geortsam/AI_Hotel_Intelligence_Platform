"""R2.10C -- the measurement path: artifacts, refusal and offline verification (§12.6-12.7).

`run_measurement` is the path the C.4 integration test calls. It gathers every input of §12.3,
checks the sign-off precondition, ingests through the harness, compares the database state
(§12.4), runs every query, checks every served result, scores with `V2_METRIC_CONFIG`, and writes
to `$RETRIEVAL_EVAL_OUT`, **atomically and before the caller asserts anything**:

- `report.json`: the manifest (fingerprint, components, inputs, sign-off) and the results;
- `environment.json`: execution metadata, never compared, with the measured commit's tree id;
- `report.json.sha256`: written last, `<sha256>  report.json`.

On any refusal of §12.7 it writes `refusal.json` instead and raises `MeasurementRefused`: there is
no fallback and no partial report. Every artifact is canonical JSON (§12.2); the writer refuses
float objects, so result values that are binary64 are written as `float.hex` strings.

The directory (§12.6) must be absolute and resolve outside the repository; otherwise the run
refuses and writes nothing at all. It holds only `docker.json` when a run starts, and a run leaves
exactly `SUCCESS_SET` or exactly `REFUSAL_SET`. Every name and both pinned paths are constants.

The workflow writes `docker.json` (`docker inspect`, `docker image inspect`, `docker info` of the
PostgreSQL service container) to the same directory before the tests, and a shard-1 step with
`if: always()` prints every file's SHA-256 and uploads the directory (§12.6).

**C.4's three requirements (§12.6).** The C.4 integration test builds its application with the
integration conftest's `create_test_app`, reads the database through `EngineFetch` over the
same conftest engine, and checks both with `require_measurement_wiring` before measuring; it
measures inside `production_sessions_forbidden`, so a request that bypasses the `get_db`
override stops the run (A). It builds `UploadRecord.hotels` with `probes.hotel_ids`, by SQL
(B). It passes every ingest and search response through `checked_json` with the status it
expects, so a failed or unexpected response refuses instead of becoming a result (C).

`verify` is pinning step 2, run offline on files you downloaded with `gh run download` (which
needs your authorisation each time; nothing here downloads). A refused run is reported first.
Otherwise the directory must hold exactly the four files of a run; the report's SHA-256 must
equal its `.sha256` file and the report digest from the job log, and `environment.json`'s must
equal the environment digest from the job log; the fingerprint must equal SHA-256(canonical(
inputs)) and every component its group. Only then is the environment's tree id trusted: the
checked-out HEAD must have exactly that tree (a pull request's merge commit qualifies only so),
and the repository-derived groups (sources, dataset, metrics) must recompute identically there.

    python -m tests.evaluation.retrieval_artifacts verify DIR
        --log-digest REPORT_SHA256 --environment-log-digest ENVIRONMENT_SHA256

No database, network or model of its own.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import inspect
import json
import os
import re
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

from sqlalchemy import Engine, text

from tests.evaluation import retrieval_canonical as canon
from tests.evaluation import retrieval_fingerprint as fp
from tests.evaluation import retrieval_probes as probes
from tests.evaluation import retrieval_review as rr
from tests.evaluation import retrieval_signoff as so
from tests.evaluation import retrieval_v2 as v2
from tests.evaluation.retrieval_canonical import Binary64
from tests.evaluation.retrieval_fingerprint import MeasurementRefused, refuse
from tests.evaluation.retrieval_metrics import (
    V2_METRIC_CONFIG,
    ConfigurationError,
    ResultsError,
    evaluate,
)
from tests.evaluation.retrieval_numerics import NumericVerificationError
from tests.evaluation.retrieval_schema import DatasetError, RetrievalDataset

REPORT_VERSION: Final = "knowledge_retrieval_report_v2"
REFUSAL_VERSION: Final = "knowledge_retrieval_refusal_v1"
OUT_VARIABLE: Final = "RETRIEVAL_EVAL_OUT"
REPORT_FILE: Final = "report.json"
ENVIRONMENT_FILE: Final = "environment.json"
DIGEST_FILE: Final = "report.json.sha256"
REFUSAL_FILE: Final = "refusal.json"
DOCKER_FILE: Final = "docker.json"
OUTPUTS: Final = (REPORT_FILE, ENVIRONMENT_FILE, DIGEST_FILE, REFUSAL_FILE)
#: What a run leaves in the directory: exactly one of these two sets (§12.6).
SUCCESS_SET: Final = frozenset({DOCKER_FILE, REPORT_FILE, ENVIRONMENT_FILE, DIGEST_FILE})
REFUSAL_SET: Final = frozenset({DOCKER_FILE, REFUSAL_FILE})
#: Where pinning (§12.6 step 3) copies the bytes; `.gitattributes` marks both `-text`.
PINNED_REPORT: Final = "tests/evaluation/retrieval_report_v2.json"
PINNED_ENVIRONMENT: Final = "tests/evaluation/retrieval_report_v2.environment.json"

SIGNOFF_FILE: Final = rr.SIGNOFF_FILE
SHEET_FILE: Final = rr.SHEET_FILE
FORM_FILE: Final = rr.FORM_FILE
DEV_LOCK: Final = "backend/requirements-dev-lock.txt"
RUNTIME_LOCK: Final = "backend/requirements-lock.txt"

_DIGEST_LINE: Final = "{digest}  " + REPORT_FILE + "\n"
_SHA256_HEX: Final = re.compile(r"[0-9a-f]{64}")
_TREE_ID: Final = re.compile(r"[0-9a-f]{40}")


# --- writing --------------------------------------------------------------------------------------


def _atomic_write(directory: Path, name: str, data: bytes) -> None:
    temporary = directory / f".{name}.partial"
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, directory / name)


def _fresh(directory: Path) -> None:
    """A run writes once, into a directory that holds `docker.json` and nothing else."""
    directory.mkdir(parents=True, exist_ok=True)
    stale = sorted(entry.name for entry in directory.iterdir() if entry.name != DOCKER_FILE)
    if stale:
        raise refuse("unmeasurable", f"{directory} already holds {stale}; a run writes once")


def write_report(directory: Path, report: Mapping[str, Any], environment: Mapping[str, Any]) -> str:
    """Write the report, its environment record, then its digest. Returns the report SHA-256."""
    _fresh(directory)
    report_bytes = canon.dumps(report)
    environment_bytes = canon.dumps(environment)
    digest = hashlib.sha256(report_bytes).hexdigest()
    _atomic_write(directory, REPORT_FILE, report_bytes)
    _atomic_write(directory, ENVIRONMENT_FILE, environment_bytes)
    _atomic_write(directory, DIGEST_FILE, _DIGEST_LINE.format(digest=digest).encode("ascii"))
    return digest


def write_refusal(
    directory: Path, refusal: MeasurementRefused, environment: Mapping[str, Any]
) -> None:
    """Write `refusal.json` and nothing else. A report already written is never replaced."""
    _fresh(directory)
    record = {
        "refusal": REFUSAL_VERSION,
        "reason": refusal.reason,
        "detail": refusal.detail,
        "environment": dict(environment),
    }
    _atomic_write(directory, REFUSAL_FILE, canon.dumps(record))


def float_free(value: object, where: str = "results") -> object:
    """Results with every float wrapped as Binary64 (written as `float.hex`). Fractions refuse."""
    if isinstance(value, float):
        return Binary64(value)
    if isinstance(value, Fraction):
        raise canon.CanonicalError(f"{where}: a Fraction has no declared form")
    if isinstance(value, Mapping):
        return {key: float_free(item, f"{where}.{key}") for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [float_free(item, f"{where}[{i}]") for i, item in enumerate(value)]
    return value


# --- the output directory (§12.6) ----------------------------------------------------------------


def output_directory_problem(path: Path, repository: Path = fp.REPOSITORY_ROOT) -> str | None:
    """Why *path* may not hold artifacts: relative, or resolving (symlinks followed) inside the
    measured repository or this one. None when it may."""
    if not path.is_absolute():
        return f"{path} is not an absolute path"
    resolved = path.resolve()
    for root in dict.fromkeys((repository.resolve(), fp.REPOSITORY_ROOT.resolve())):
        if resolved.is_relative_to(root):
            return f"{path} resolves to {resolved}, inside the repository {root}"
    return None


def require_output_directory(path: Path, repository: Path = fp.REPOSITORY_ROOT) -> Path:
    """*path*, or a refusal (`unmeasurable`) that writes nothing, since nowhere is safe to."""
    problem = output_directory_problem(path, repository)
    if problem is not None:
        raise refuse("unmeasurable", f"{OUT_VARIABLE}: {problem}; nothing was written")
    return path


# --- the measurement path -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Harness:
    """What the C.4 integration test supplies: database reads, ingestion and search."""

    fetch: probes.Fetch
    #: Uploads the dataset by `probes.plan`, then withdraws; returns what the API answered.
    ingest: Callable[[RetrievalDataset], probes.UploadRecord]
    #: `GET .../knowledge/search?q=<query>&limit=<limit>`: the response's JSON.
    search: Callable[[str, int], Mapping[str, Any]]


# --- C.4's requirements (§12.6) -------------------------------------------------------------------


class EngineFetch:
    """`probes.Fetch` over one SQLAlchemy engine: the integration conftest's (requirement A)."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def __call__(self, sql: str, params: Mapping[str, Any]) -> list[tuple[Any, ...]]:
        with self.engine.connect() as connection:
            return [tuple(row) for row in connection.execute(text(sql), dict(params))]


def require_measurement_wiring(app: Any, fetch: object) -> None:
    """Requirement A: requests reach the database only through the conftest's `get_db`
    override, and that override's sessions are bound to the very engine `fetch` reads."""
    from app.api.deps import get_db  # deferred: the application is C.4's, not the harness's

    override = getattr(app, "dependency_overrides", {}).get(get_db)
    if override is None:
        raise refuse("unmeasurable", "get_db is not overridden: requests would use production's")
    engine = getattr(fetch, "engine", None)
    if not isinstance(engine, Engine):
        raise refuse("unmeasurable", "fetch is not bound to an engine; use EngineFetch")
    made = override()
    try:
        session: Any = next(made) if inspect.isgenerator(made) else made
        bind = session.get_bind()
    finally:
        if inspect.isgenerator(made):
            made.close()
    if bind is not engine:
        raise refuse("unmeasurable", "the get_db override and fetch use different engines")


@contextmanager
def production_sessions_forbidden() -> Iterator[None]:
    """Requirement A: while measuring, the production session factory refuses, so a request
    that bypasses the `get_db` override stops the run instead of falling back."""
    from app.api import deps  # deferred, as above

    def forbidden() -> Any:
        raise refuse("unmeasurable", "the production session factory was reached")

    original = deps.get_session_factory
    setattr(deps, "get_session_factory", forbidden)  # noqa: B010 - a module attribute
    try:
        yield
    finally:
        setattr(deps, "get_session_factory", original)  # noqa: B010


def checked_json(response: Any, expected_status: int, what: str) -> dict[str, Any]:
    """Requirement C: the response's JSON object, or a refusal when the status is not the one
    expected or the body is not a JSON object. Nothing failed becomes a result."""
    status = getattr(response, "status_code", None)
    if isinstance(status, bool) or status != expected_status:
        raise refuse("unmeasurable", f"{what}: HTTP {status}, expected {expected_status}")
    try:
        body = response.json()
    except Exception as exc:
        raise refuse("unmeasurable", f"{what}: the body is not JSON") from exc
    if not isinstance(body, dict):
        raise refuse("unmeasurable", f"{what}: the body is not a JSON object")
    return body


SETTINGS_SQL: Final = (
    "SELECT name, setting, source FROM pg_settings "
    "WHERE source <> 'default' OR name = 'search_path' ORDER BY name"
)


def database_metadata(fetch: probes.Fetch) -> dict[str, Any] | None:
    """Raw PostgreSQL settings for the environment record; metadata, so a failure is not fatal."""
    try:
        rows = fetch(SETTINGS_SQL, {})
    except Exception:
        return None
    return {"settings": [[str(item) for item in row] for row in rows]}


def _text(git: fp.GitRunner, commit: str, path: str) -> str:
    try:
        return fp.blob_bytes(git, commit, path).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise refuse("repository", f"{path} at {commit} is not UTF-8") from exc


@dataclass(frozen=True, slots=True)
class RepositoryInputs:
    commit: str
    git: fp.GitRunner
    dataset: RetrievalDataset
    sources: dict[str, Any]


def repository_inputs(
    repository: Path, *, require_imports: bool = True, require_no_strays: bool = True
) -> RepositoryInputs:
    """The commit, a clean tree without strays, the sources at that commit, and the dataset
    (§12.6). `verify` recomputes from blobs alone, so it needs neither imports nor stray checks."""
    git = fp.git_runner(repository)
    commit = fp.measured_commit(git)
    fp.require_clean(git)
    if require_no_strays:
        fp.require_no_strays(git, repository)
    sources = fp.sources_group(git, commit, repository)
    if require_imports:
        fp.require_repository_imports(repository, sources)
    try:
        dataset = v2.load(repository / v2.DATASET_PATH.relative_to(v2.REPOSITORY_ROOT))
    except (DatasetError, ValueError) as exc:
        raise refuse("mismatch", f"the dataset is not the pinned one: {exc}") from exc
    return RepositoryInputs(commit, git, dataset, sources)


def run_measurement(
    harness: Harness,
    out_dir: Path,
    *,
    repository: Path = fp.REPOSITORY_ROOT,
    environ: Mapping[str, str] | None = None,
    installed: Mapping[str, str] | None = None,
    require_imports: bool = True,
) -> dict[str, Any]:
    """Measure once. Writes the report (or `refusal.json`) before returning or raising; an
    output directory that may not hold artifacts refuses before anything is written."""
    require_output_directory(out_dir, repository)
    env = os.environ if environ is None else environ
    started = datetime.datetime.now(datetime.UTC)
    commit: str | None = None
    tree: str | None = None
    fetch_for_metadata: probes.Fetch | None = None
    try:
        commit = fp.measured_commit(fp.git_runner(repository))
        tree = fp.commit_tree(fp.git_runner(repository), commit)
        found = repository_inputs(repository, require_imports=require_imports)
        if found.commit != commit:
            raise refuse("repository", f"HEAD moved from {commit} to {found.commit}")
        git, dataset = found.git, found.dataset
        precondition = fp.signoff_precondition(
            dataset,
            record_text=_text(git, commit, SIGNOFF_FILE),
            sheet_text=_text(git, commit, SHEET_FILE),
            form_text=_text(git, commit, FORM_FILE),
            decision_record_text=_text(git, commit, so.DECISION_RECORD_FILE),
        )
        image = fp.declared_image(
            fp.blob_bytes(git, commit, fp.CI_FILE), env.get(fp.IMAGE_VARIABLE)
        )
        docker = fp.docker_identity(read_docker_metadata(out_dir), image)
        python = fp.python_group(
            _text(git, commit, DEV_LOCK), _text(git, commit, RUNTIME_LOCK), installed
        )
        metrics = fp.metrics_group()

        fetch_for_metadata = harness.fetch
        uploads = harness.ingest(dataset)
        state = probes.read_state(harness.fetch, uploads)
        probes.compare_state(state, dataset)
        number, version, extensions = probes.server(harness.fetch)
        configurations = sorted({document.language for document in dataset.documents})
        inputs = {
            "engine": fp.engine_group(docker, number, version, extensions),
            "database": probes.database_group(harness.fetch),
            "text_search": probes.text_search_group(harness.fetch, configurations),
            "document_state": state.documents,
            "chunk_state": state.chunks,
            "query_parsing": probes.query_parsing_group(harness.fetch, dataset),
            "schema": probes.schema_group(harness.fetch),
            "sources": found.sources,
            "python": python,
            "dataset": fp.dataset_group(dataset),
            "metrics": metrics,
        }
        manifest = fp.build_manifest(inputs, precondition)

        served = {
            query.query_id: probes.served_results(
                harness.search(query.query, query.limit),
                query=query.query,
                limit=query.limit,
                state=state,
                dataset=dataset,
            )
            for query in dataset.queries
        }
        ranked = {
            qid: [(row["doc_id"], row["ordinal"]) for row in rows] for qid, rows in served.items()
        }
        try:
            scored = evaluate(dataset, ranked, V2_METRIC_CONFIG)
        except NumericVerificationError as exc:
            raise refuse("numeric_certificate", str(exc)) from exc
        except (ResultsError, ConfigurationError) as exc:
            raise refuse("mismatch", f"results do not match the dataset: {exc}") from exc
        report = {
            "report": REPORT_VERSION,
            **manifest,
            "results": {"metrics": float_free(scored), "served": served},
        }
        environment = fp.environment_record(
            commit,
            tree=tree,
            started=started,
            finished=datetime.datetime.now(datetime.UTC),
            database=database_metadata(harness.fetch),
            environ=env,
        )
        write_report(out_dir, report, environment)
        return report
    except MeasurementRefused as refusal:
        _record_refusal(out_dir, refusal, commit, tree, started, fetch_for_metadata, env)
        raise
    except Exception as exc:
        failure = refuse("unmeasurable", f"the measurement failed: {exc!r}")
        _record_refusal(out_dir, failure, commit, tree, started, fetch_for_metadata, env)
        raise failure from exc


def _record_refusal(
    out_dir: Path,
    refusal: MeasurementRefused,
    commit: str | None,
    tree: str | None,
    started: datetime.datetime,
    fetch: probes.Fetch | None,
    env: Mapping[str, str],
) -> None:
    environment = fp.environment_record(
        commit,
        tree=tree,
        started=started,
        finished=datetime.datetime.now(datetime.UTC),
        database=None if fetch is None else database_metadata(fetch),
        environ=env,
    )
    write_refusal(out_dir, refusal, environment)


def read_docker_metadata(directory: Path) -> dict[str, Any]:
    path = directory / DOCKER_FILE
    try:
        metadata = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise refuse("image_identity", f"{path} cannot be read: {exc!r}") from exc
    if not isinstance(metadata, dict):
        raise refuse("image_identity", f"{path} is not an object")
    return metadata


def out_directory(
    environ: Mapping[str, str] | None = None, repository: Path = fp.REPOSITORY_ROOT
) -> Path:
    """`$RETRIEVAL_EVAL_OUT`, which the workflow sets in shard 1 to `$RUNNER_TEMP/retrieval-eval`.
    Unset, relative or inside the repository: refused, with nothing written."""
    value = (os.environ if environ is None else environ).get(OUT_VARIABLE)
    if not value:
        raise refuse("unmeasurable", f"{OUT_VARIABLE} is not set")
    return require_output_directory(Path(value), repository)


# --- verifying downloaded artifacts (§12.6 "Pinning" step 2) --------------------------------------

REPORT_KEYS: Final = frozenset({"report", "results", *(fp.MANIFEST_SPEC.fields or {})})


def report_problems(report: Mapping[str, Any]) -> list[str]:
    """Structure, fingerprint and components of a report read back from canonical JSON."""
    if set(report) != REPORT_KEYS:
        return [f"report keys must be {sorted(REPORT_KEYS)}"]
    problems = [] if report["report"] == REPORT_VERSION else [f"report must be {REPORT_VERSION}"]
    manifest = {key: report[key] for key in fp.MANIFEST_SPEC.fields or {}}
    return problems + fp.manifest_problems(manifest)


def artifact_set_problems(directory: Path) -> list[str]:
    """A refused run is reported first; otherwise exactly `SUCCESS_SET`, as regular files."""
    if not directory.is_dir():
        return [f"{directory} is not a directory"]
    entries = {entry.name: entry for entry in directory.iterdir()}
    names = set(entries)
    if REFUSAL_FILE in names:
        problems = ["the run refused; there is no report to pin"]
        if names != REFUSAL_SET:
            problems.append(
                f"a refused run holds exactly {sorted(REFUSAL_SET)}; found {sorted(names)}"
            )
        return problems
    problems = []
    missing = sorted(SUCCESS_SET - names)
    if missing:
        problems.append(f"missing {missing}")
    extra = sorted(names - SUCCESS_SET)
    if extra:
        problems.append(f"unexpected files {extra}")
    irregular = sorted(
        name for name, entry in entries.items() if entry.is_symlink() or not entry.is_file()
    )
    if irregular:
        problems.append(f"not regular files {irregular}")
    return problems


def digest_problems(directory: Path, log_digest: str) -> list[str]:
    """The report's SHA-256 must equal both its `.sha256` file and the digest in the job log."""
    report_bytes = (directory / REPORT_FILE).read_bytes()
    digest = hashlib.sha256(report_bytes).hexdigest()
    problems = []
    recorded = (directory / DIGEST_FILE).read_bytes()
    if recorded != _DIGEST_LINE.format(digest=digest).encode("ascii"):
        problems.append(f"{DIGEST_FILE} does not hold the report's SHA-256 {digest}")
    if log_digest != digest:
        problems.append(f"the job log's digest {log_digest} is not the report's {digest}")
    return problems


def environment_digest_problems(directory: Path, environment_log_digest: str) -> list[str]:
    """`environment.json`'s SHA-256 must equal the environment digest in the job log."""
    if not _SHA256_HEX.fullmatch(environment_log_digest):
        return [f"no environment digest from the job log: {environment_log_digest!r}"]
    digest = hashlib.sha256((directory / ENVIRONMENT_FILE).read_bytes()).hexdigest()
    if environment_log_digest != digest:
        return [
            f"the job log's environment digest {environment_log_digest} is not "
            f"{ENVIRONMENT_FILE}'s {digest}"
        ]
    return []


def repository_problems(
    report: Mapping[str, Any], environment: Mapping[str, Any], repository: Path
) -> list[str]:
    """HEAD must have the run's tree, and the repository-derived groups must recompute there.

    The tree id comes from `environment.json`, trusted only after its digest matched the job
    log. A commit with that exact tree passes whatever its id, branch or message.
    """
    tree = environment.get("tree")
    if not isinstance(tree, str) or not _TREE_ID.fullmatch(tree):
        return [f"{ENVIRONMENT_FILE} records no tree id: {tree!r}"]
    found = repository_inputs(repository, require_imports=False, require_no_strays=False)
    head_tree = fp.commit_tree(found.git, found.commit)
    if head_tree != tree:
        return [
            f"check out a commit whose tree is the run's {tree}; HEAD {found.commit} has "
            f"tree {head_tree}"
        ]
    recomputed = {
        "sources": found.sources,
        "dataset": fp.dataset_group(found.dataset),
        "metrics": fp.metrics_group(),
    }
    problems = []
    for group in fp.REPOSITORY_GROUPS:
        if canon.sha256(recomputed[group]) != report["components"][group]:
            problems.append(f"{group} does not recompute at {found.commit}")
    return problems


def verify(
    directory: Path,
    log_digest: str,
    environment_log_digest: str,
    repository: Path = fp.REPOSITORY_ROOT,
) -> list[str]:
    """Every check of pinning step 2. An empty list is the only pass."""
    problems = artifact_set_problems(directory)
    if problems:
        return problems
    problems = digest_problems(directory, log_digest)
    untrusted = environment_digest_problems(directory, environment_log_digest)
    problems += untrusted
    try:
        report = canon.loads((directory / REPORT_FILE).read_bytes())
        environment = canon.loads((directory / ENVIRONMENT_FILE).read_bytes())
    except canon.CanonicalError as exc:
        return [*problems, f"not canonical: {exc}"]
    found = report_problems(report)
    if found:
        return problems + found
    if untrusted:
        return problems  # the tree id is unproven, so nothing is checked out against it
    try:
        problems += repository_problems(report, environment, repository)
    except MeasurementRefused as exc:
        problems.append(f"cannot recompute at the run's commit: {exc}")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.evaluation.retrieval_artifacts")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("verify", help="pinning step 2, offline, on downloaded files")
    check.add_argument("directory", type=Path)
    check.add_argument("--log-digest", required=True, help="the report SHA-256 from the job log")
    check.add_argument(
        "--environment-log-digest",
        required=True,
        help="the environment.json SHA-256 from the job log",
    )
    check.add_argument("--repository", type=Path, default=fp.REPOSITORY_ROOT)
    arguments = parser.parse_args(argv)
    problems = verify(
        arguments.directory,
        arguments.log_digest,
        arguments.environment_log_digest,
        arguments.repository,
    )
    for problem in problems:
        print(f"FAIL {problem}")
    if not problems:
        print("OK   files, digests, fingerprint, components, tree and repository groups verify")
    return 1 if problems else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = [
    "DIGEST_FILE",
    "DOCKER_FILE",
    "ENVIRONMENT_FILE",
    "OUTPUTS",
    "OUT_VARIABLE",
    "PINNED_ENVIRONMENT",
    "PINNED_REPORT",
    "REFUSAL_FILE",
    "REFUSAL_SET",
    "REFUSAL_VERSION",
    "REPORT_FILE",
    "REPORT_VERSION",
    "SUCCESS_SET",
    "EngineFetch",
    "Harness",
    "RepositoryInputs",
    "artifact_set_problems",
    "checked_json",
    "database_metadata",
    "digest_problems",
    "environment_digest_problems",
    "float_free",
    "main",
    "out_directory",
    "output_directory_problem",
    "production_sessions_forbidden",
    "read_docker_metadata",
    "report_problems",
    "repository_inputs",
    "repository_problems",
    "require_measurement_wiring",
    "require_output_directory",
    "run_measurement",
    "verify",
    "write_refusal",
    "write_report",
]
