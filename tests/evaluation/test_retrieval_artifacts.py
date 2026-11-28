"""R2.10C -- decision F §12.6-12.7: the measurement path, its artifacts, refusals and verification.

Offline. The measurement runs end to end against `fingerprint_fixtures`: a throwaway Git repository
holding copies of every fingerprinted file, a fake PostgreSQL answering the probes' SQL, and a
simulated ingestion and search. The workflow steps of §12.6 are checked in `ci.yml` as written.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

from tests.evaluation import fingerprint_fixtures as ff
from tests.evaluation import retrieval_artifacts as ra
from tests.evaluation import retrieval_canonical as canon
from tests.evaluation import retrieval_fingerprint as fp
from tests.evaluation import retrieval_probes as rp
from tests.evaluation import retrieval_v2 as v2
from tests.evaluation.retrieval_canonical import Binary64
from tests.evaluation.retrieval_fingerprint import MeasurementRefused
from tests.evaluation.retrieval_schema import RetrievalDataset

ROOT = fp.REPOSITORY_ROOT


@pytest.fixture(scope="module")
def dataset() -> RetrievalDataset:
    return v2.load()


@pytest.fixture(scope="module")
def repository(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return ff.make_repository(tmp_path_factory.mktemp("artifacts") / "repo")


def measure(
    dataset: RetrievalDataset,
    repository: Path,
    out: Path,
    *,
    db: ff.FakeDatabase | None = None,
    environ: dict[str, str] | None = None,
    harness: ra.Harness | None = None,
) -> dict[str, Any]:
    if not (out / ra.DOCKER_FILE).exists():
        ff.write_docker(out)
    return ra.run_measurement(
        harness or ff.harness(dataset, db),
        out,
        repository=repository,
        environ=ff.environ() if environ is None else environ,
        installed=ff.installed_from_lock(),
        require_imports=False,
    )


def raise_(error: Exception) -> Any:
    raise error


def refusal_of(out: Path) -> dict[str, Any]:
    record = canon.loads((out / ra.REFUSAL_FILE).read_bytes())
    assert not (out / ra.REPORT_FILE).exists()
    assert not (out / ra.DIGEST_FILE).exists()
    return record


# --- the measurement, end to end ------------------------------------------------------------------


@pytest.fixture(scope="module")
def measured(
    dataset: RetrievalDataset, repository: Path, tmp_path_factory: pytest.TempPathFactory
) -> tuple[dict[str, Any], Path]:
    out = tmp_path_factory.mktemp("out")
    return measure(dataset, repository, out), out


def test_a_measurement_writes_report_environment_and_digest(
    measured: tuple[dict[str, Any], Path],
) -> None:
    report, out = measured
    assert sorted(p.name for p in out.iterdir()) == [
        ra.DOCKER_FILE,
        ra.ENVIRONMENT_FILE,
        ra.REPORT_FILE,
        ra.DIGEST_FILE,
    ]
    data = (out / ra.REPORT_FILE).read_bytes()
    assert data == canon.dumps(report)
    digest = hashlib.sha256(data).hexdigest()
    assert (out / ra.DIGEST_FILE).read_bytes() == f"{digest}  report.json\n".encode()
    assert ra.report_problems(canon.loads(data)) == []


def test_the_report_carries_the_manifest_and_the_results(
    measured: tuple[dict[str, Any], Path], dataset: RetrievalDataset
) -> None:
    report, _out = measured
    assert set(report) == ra.REPORT_KEYS
    assert report["report"] == ra.REPORT_VERSION
    assert report["fingerprint"] == canon.sha256(report["inputs"])
    assert report["signoff"]["decision"] == "approved"
    assert report["inputs"]["dataset"]["sha256"] == v2.DATASET_SHA256
    assert report["inputs"]["engine"]["declared_image"]["digest"] == ff.DIGEST
    served = report["results"]["served"]
    assert sorted(served) == sorted(q.query_id for q in dataset.queries)
    for query in dataset.queries:
        assert len(served[query.query_id]) <= query.limit
    metrics = report["results"]["metrics"]
    assert metrics["dataset"]["sha256"] == v2.DATASET_SHA256
    assert metrics["configuration"]["fixed"]["z"] == Binary64(1.959963984540054)


def test_the_environment_record_is_separate_and_never_in_the_fingerprint(
    measured: tuple[dict[str, Any], Path], repository: Path
) -> None:
    report, out = measured
    environment = canon.loads((out / ra.ENVIRONMENT_FILE).read_bytes())
    assert environment["commit"] == ff.git(repository, "rev-parse", "HEAD")
    assert environment["tree"] == ff.git(repository, "rev-parse", "HEAD^{tree}")
    assert environment["pythonhashseed"] == "0"
    assert "environment" not in report
    assert b"commit" not in canon.dumps(report["inputs"])


def test_the_same_inputs_give_the_same_fingerprint_and_results(
    measured: tuple[dict[str, Any], Path],
    dataset: RetrievalDataset,
    repository: Path,
    tmp_path: Path,
) -> None:
    report, _out = measured
    again = measure(dataset, repository, tmp_path / "again", environ=ff.environ(PYTHONHASHSEED="1"))
    assert fp.compare(report, again) is None
    assert canon.dumps(report) == canon.dumps(again)


def test_a_different_input_is_reported_as_inputs_changed(
    measured: tuple[dict[str, Any], Path],
    dataset: RetrievalDataset,
    repository: Path,
    tmp_path: Path,
) -> None:
    report, _out = measured
    db = ff.database(dataset)
    db.overrides[rp.EXTENSIONS_SQL] = lambda _p: [("plpgsql", "1.0")]
    changed = measure(dataset, repository, tmp_path / "changed", db=db)
    assert fp.compare(report, changed) == "inputs changed: engine"


def test_different_results_with_the_same_inputs_are_reported_as_such(
    measured: tuple[dict[str, Any], Path],
    dataset: RetrievalDataset,
    repository: Path,
    tmp_path: Path,
) -> None:
    report, _out = measured
    db = ff.database(dataset)
    search = ff.search(dataset, db.ingested)

    def fewer(text: str, limit: int) -> dict[str, Any]:
        response = search(text, limit)
        response["results"] = response["results"][:1]
        return response

    harness = ra.Harness(fetch=db, ingest=lambda _d: db.ingested.uploads, search=fewer)
    changed = measure(dataset, repository, tmp_path / "fewer", harness=harness)
    assert fp.compare(report, changed) == "results changed with unchanged inputs"


# --- refusals: refusal.json, no report, an exception ----------------------------------------------


def test_a_dirty_tree_refuses_and_writes_only_refusal_json(
    dataset: RetrievalDataset, tmp_path: Path
) -> None:
    repo = ff.make_repository(tmp_path / "dirty")
    (repo / "alembic.ini").write_bytes(b"# edited\n")
    out = tmp_path / "out"
    with pytest.raises(MeasurementRefused, match="dirty"):
        measure(dataset, repo, out)
    record = refusal_of(out)
    assert record["reason"] == "repository"
    assert record["refusal"] == ra.REFUSAL_VERSION
    assert record["environment"]["commit"] == ff.git(repo, "rev-parse", "HEAD")


def test_an_untracked_file_refuses_the_measurement(
    dataset: RetrievalDataset, tmp_path: Path
) -> None:
    repo = ff.make_repository(tmp_path / "stray")
    (repo / "conftest.py").write_text("import os\n", encoding="utf-8")
    out = tmp_path / "out"
    with pytest.raises(MeasurementRefused, match="untracked files"):
        measure(dataset, repo, out)
    record = refusal_of(out)
    assert record["reason"] == "repository"
    assert record["environment"]["tree"] == ff.git(repo, "rev-parse", "HEAD^{tree}")


@pytest.mark.parametrize(("sql", "column"), [("TEMPLATE_SQL", 3), ("TOKEN_TYPES_SQL", 2)])
def test_an_empty_lexize_or_token_description_from_the_server_refuses(
    dataset: RetrievalDataset, repository: Path, tmp_path: Path, sql: str, column: int
) -> None:
    db = ff.database(dataset)
    statement = getattr(rp, sql)

    def emptied(params: Any) -> list[tuple[Any, ...]]:
        rows = [list(row) for row in db.answer(statement, params)]
        for row in rows:
            row[column] = ""
        return [tuple(row) for row in rows]

    db.overrides[statement] = emptied
    out = tmp_path / "out"
    with pytest.raises(MeasurementRefused, match="declared type"):
        measure(dataset, repository, out, db=db)
    assert refusal_of(out)["reason"] == "unmeasurable"


def test_a_missing_c4_integration_test_refuses(dataset: RetrievalDataset, tmp_path: Path) -> None:
    repo = ff.make_repository(tmp_path / "noc4", integration_test=False)
    out = tmp_path / "out"
    with pytest.raises(MeasurementRefused, match="sources missing"):
        measure(dataset, repo, out)
    assert fp.INTEGRATION_TEST in refusal_of(out)["detail"]


@pytest.mark.parametrize(
    ("metadata", "environ", "reason"),
    [
        (ff.docker_metadata(image__RepoDigests=[]), None, "image_identity"),
        (None, {"PYTHONHASHSEED": "0"}, "image_identity"),
        (ff.docker_metadata(container__Image="sha256:" + "0" * 64), None, "image_identity"),
    ],
)
def test_an_image_identity_failure_refuses(
    dataset: RetrievalDataset,
    repository: Path,
    tmp_path: Path,
    metadata: dict[str, Any] | None,
    environ: dict[str, str] | None,
    reason: str,
) -> None:
    out = tmp_path / "out"
    ff.write_docker(out, metadata)
    with pytest.raises(MeasurementRefused):
        measure(dataset, repository, out, environ=environ)
    assert refusal_of(out)["reason"] == reason


def test_a_missing_docker_metadata_file_refuses(
    dataset: RetrievalDataset, repository: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(MeasurementRefused):
        ra.run_measurement(
            ff.harness(dataset),
            out,
            repository=repository,
            environ=ff.environ(),
            installed=ff.installed_from_lock(),
            require_imports=False,
        )
    assert refusal_of(out)["reason"] == "image_identity"


def test_an_installed_set_failure_refuses(
    dataset: RetrievalDataset, repository: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    ff.write_docker(out)
    with pytest.raises(MeasurementRefused):
        ra.run_measurement(
            ff.harness(dataset),
            out,
            repository=repository,
            environ=ff.environ(),
            installed={**ff.installed_from_lock(), "unpinned-extra": "1.0"},
            require_imports=False,
        )
    assert refusal_of(out)["reason"] == "installed_set"


def test_a_sign_off_mismatch_refuses(dataset: RetrievalDataset, tmp_path: Path) -> None:
    repo = ff.make_repository(tmp_path / "signoff")
    path = repo / ff.rr.SIGNOFF_FILE
    path.write_bytes(path.read_bytes().replace(b'"approved"', b'"rejected"'))
    ff.git(repo, "commit", "-qam", "reject")
    out = tmp_path / "out"
    with pytest.raises(MeasurementRefused):
        measure(dataset, repo, out)
    assert refusal_of(out)["reason"] == "signoff"


@pytest.mark.parametrize(
    ("sql", "answer", "reason"),
    [
        (rp.SERVER_SQL, lambda _p: [(180006, None)], "mismatch"),
        (rp.DATABASE_SQL, lambda _p: (_ for _ in ()).throw(RuntimeError("down")), "unmeasurable"),
        (
            rp.STOPWORD_FILE_SQL,
            lambda _p: (_ for _ in ()).throw(RuntimeError("denied")),
            "stopwords",
        ),
    ],
)
def test_a_probe_failure_refuses(
    dataset: RetrievalDataset,
    repository: Path,
    tmp_path: Path,
    sql: str,
    answer: Any,
    reason: str,
) -> None:
    db = ff.database(dataset)
    db.overrides[sql] = answer
    out = tmp_path / "out"
    with pytest.raises(MeasurementRefused):
        measure(dataset, repository, out, db=db)
    assert refusal_of(out)["reason"] == reason


def test_a_state_mismatch_refuses(
    dataset: RetrievalDataset, repository: Path, tmp_path: Path
) -> None:
    db = ff.database(dataset)
    db.ingested.documents[0][5] = "Renamed"
    out = tmp_path / "out"
    with pytest.raises(MeasurementRefused):
        measure(dataset, repository, out, db=db)
    assert refusal_of(out)["reason"] == "mismatch"


def test_a_served_result_mismatch_refuses(
    dataset: RetrievalDataset, repository: Path, tmp_path: Path
) -> None:
    db = ff.database(dataset)
    search = ff.search(dataset, db.ingested)

    def wrong_echo(text: str, limit: int) -> dict[str, Any]:
        return {**search(text, limit), "limit": limit + 1}

    harness = ra.Harness(fetch=db, ingest=lambda _d: db.ingested.uploads, search=wrong_echo)
    out = tmp_path / "out"
    with pytest.raises(MeasurementRefused):
        measure(dataset, repository, out, harness=harness)
    assert refusal_of(out)["reason"] == "served"


def test_an_unexpected_failure_refuses_as_unmeasurable(
    dataset: RetrievalDataset, repository: Path, tmp_path: Path
) -> None:
    db = ff.database(dataset)

    def broken(_text: str, _limit: int) -> dict[str, Any]:
        raise ConnectionError("the API went away")

    harness = ra.Harness(fetch=db, ingest=lambda _d: db.ingested.uploads, search=broken)
    out = tmp_path / "out"
    with pytest.raises(MeasurementRefused, match="ConnectionError"):
        measure(dataset, repository, out, harness=harness)
    assert refusal_of(out)["reason"] == "unmeasurable"


def test_a_numeric_certificate_failure_refuses(
    dataset: RetrievalDataset, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> None:
        raise fp.NumericVerificationError("certificate failed")

    monkeypatch.setattr(fp, "discount_table", broken)
    out = tmp_path / "out"
    with pytest.raises(MeasurementRefused):
        measure(dataset, repository, out)
    assert refusal_of(out)["reason"] == "numeric_certificate"


def test_a_run_writes_once(dataset: RetrievalDataset, repository: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    measure(dataset, repository, out)
    before = (out / ra.REPORT_FILE).read_bytes()
    with pytest.raises(MeasurementRefused, match="writes once"):
        measure(dataset, repository, out)
    assert (out / ra.REPORT_FILE).read_bytes() == before


def test_a_directory_holding_anything_but_docker_json_refuses_before_writing(
    tmp_path: Path,
) -> None:
    out = tmp_path / "out"
    ff.write_docker(out)
    (out / ".report.json.partial").write_bytes(b"{")
    with pytest.raises(MeasurementRefused, match="writes once"):
        ra.write_report(out, {"a": 1}, {"b": 2})
    assert sorted(p.name for p in out.iterdir()) == [".report.json.partial", ra.DOCKER_FILE]


def test_the_digest_is_written_last(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    original = ra._atomic_write

    def spy(directory: Path, name: str, data: bytes) -> None:
        order.append(name)
        original(directory, name, data)

    monkeypatch.setattr(ra, "_atomic_write", spy)
    ra.write_report(tmp_path, {"a": 1}, {"b": 2})
    assert order == [ra.REPORT_FILE, ra.ENVIRONMENT_FILE, ra.DIGEST_FILE]
    assert not list(tmp_path.glob(".*.partial"))


def test_the_writer_refuses_floats_in_a_report(tmp_path: Path) -> None:
    with pytest.raises(canon.CanonicalError):
        ra.write_report(tmp_path, {"value": 0.5}, {})
    assert ra.float_free({"v": [0.5, 1, None]}) == {"v": [Binary64(0.5), 1, None]}
    with pytest.raises(canon.CanonicalError):
        ra.float_free({"v": ra.Fraction(1, 3)})


def test_the_output_directory_comes_from_the_workflow_variable(tmp_path: Path) -> None:
    out = tmp_path / "retrieval-eval"
    assert ra.out_directory({ra.OUT_VARIABLE: str(out)}) == out
    with pytest.raises(MeasurementRefused, match="RETRIEVAL_EVAL_OUT"):
        ra.out_directory({})


def refused_directory(problem: str) -> Any:
    return pytest.raises(
        MeasurementRefused, match=f"^unmeasurable: RETRIEVAL_EVAL_OUT: .*{problem}"
    )


def test_a_relative_output_directory_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # outside every repository: only relativity is wrong
    with refused_directory("not an absolute path"):
        ra.out_directory({ra.OUT_VARIABLE: "retrieval-eval"})
    with refused_directory("not an absolute path"):
        ra.require_output_directory(Path("retrieval-eval"))


def test_an_output_directory_inside_the_repository_refuses(repository: Path) -> None:
    for inside in (ROOT, ROOT / "tests" / "evaluation" / "out", repository / "out"):
        with refused_directory("inside the repository"):
            ra.require_output_directory(inside, repository)
    with refused_directory("inside the repository"):
        ra.out_directory({ra.OUT_VARIABLE: str(ROOT / "artifacts")})


def link_directory(link: Path, target: Path) -> None:
    """A directory symlink, or on Windows without the privilege a junction, which also resolves."""
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        import _winapi

        _winapi.CreateJunction(str(target), str(link))


def test_a_symlink_that_resolves_inside_the_repository_refuses(
    repository: Path, tmp_path: Path
) -> None:
    link = tmp_path / "looks-outside"
    link_directory(link, repository)
    assert not (link / "out").resolve().is_relative_to(tmp_path)
    with refused_directory("inside the repository"):
        ra.require_output_directory(link / "out", repository)
    ra.require_output_directory(tmp_path / "plain", repository)


def test_a_measurement_into_the_repository_refuses_and_writes_nothing(
    dataset: RetrievalDataset, repository: Path
) -> None:
    out = repository / "artifacts"
    with refused_directory("inside the repository"):
        ra.run_measurement(
            ff.harness(dataset),
            out,
            repository=repository,
            environ=ff.environ(),
            installed=ff.installed_from_lock(),
            require_imports=False,
        )
    assert not out.exists()
    assert ff.git(repository, "status", "--porcelain", "--untracked-files=all") == ""


# --- verifying downloaded artifacts (§12.6 "Pinning" step 2) --------------------------------------


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def downloaded(measured: tuple[dict[str, Any], Path], tmp_path: Path) -> tuple[Path, str, str]:
    """A copy of the artifact, with the report and environment digests the job log prints."""
    _report, out = measured
    copy = tmp_path / "download"
    shutil.copytree(out, copy)
    return copy, sha256_of(copy / ra.REPORT_FILE), sha256_of(copy / ra.ENVIRONMENT_FILE)


def test_untouched_artifacts_verify(
    measured: tuple[dict[str, Any], Path], repository: Path, tmp_path: Path
) -> None:
    directory, digest, environment_digest = downloaded(measured, tmp_path)
    assert ra.verify(directory, digest, environment_digest, repository) == []


@pytest.mark.parametrize(
    "extra", ["notes.txt", ".report.json.partial", ra.REFUSAL_FILE + ".old", "docker.json~"]
)
def test_a_success_directory_holds_exactly_its_four_files(
    measured: tuple[dict[str, Any], Path], repository: Path, tmp_path: Path, extra: str
) -> None:
    directory, digest, environment_digest = downloaded(measured, tmp_path)
    (directory / extra).write_bytes(b"{}")
    assert ra.verify(directory, digest, environment_digest, repository) == [
        f"unexpected files {[extra]}"
    ]


@pytest.mark.parametrize("name", [ra.DOCKER_FILE, ra.ENVIRONMENT_FILE, ra.DIGEST_FILE])
def test_a_success_directory_missing_a_file_fails(
    measured: tuple[dict[str, Any], Path], repository: Path, tmp_path: Path, name: str
) -> None:
    directory, digest, environment_digest = downloaded(measured, tmp_path)
    (directory / name).unlink()
    assert ra.verify(directory, digest, environment_digest, repository) == [f"missing {[name]}"]


def test_an_artifact_that_is_not_a_regular_file_fails(
    measured: tuple[dict[str, Any], Path], repository: Path, tmp_path: Path
) -> None:
    directory, digest, environment_digest = downloaded(measured, tmp_path)
    (directory / ra.DOCKER_FILE).unlink()
    (directory / ra.DOCKER_FILE).mkdir()
    assert ra.verify(directory, digest, environment_digest, repository) == [
        f"not regular files {[ra.DOCKER_FILE]}"
    ]


def test_a_wrong_log_digest_or_digest_file_fails(
    measured: tuple[dict[str, Any], Path], repository: Path, tmp_path: Path
) -> None:
    directory, digest, environment_digest = downloaded(measured, tmp_path)
    assert ra.verify(directory, "0" * 64, environment_digest, repository) == [
        f"the job log's digest {'0' * 64} is not the report's {digest}"
    ]
    (directory / ra.DIGEST_FILE).write_bytes(f"{digest} report.json\n".encode())
    assert ra.verify(directory, digest, environment_digest, repository) == [
        f"{ra.DIGEST_FILE} does not hold the report's SHA-256 {digest}"
    ]


def test_a_wrong_or_missing_environment_log_digest_fails(
    measured: tuple[dict[str, Any], Path], repository: Path, tmp_path: Path
) -> None:
    directory, digest, environment_digest = downloaded(measured, tmp_path)
    assert ra.verify(directory, digest, "0" * 64, repository) == [
        f"the job log's environment digest {'0' * 64} is not "
        f"{ra.ENVIRONMENT_FILE}'s {environment_digest}"
    ]
    for absent in ("", "none", environment_digest.upper()):
        assert ra.verify(directory, digest, absent, repository) == [
            f"no environment digest from the job log: {absent!r}"
        ]


def test_a_tampered_environment_record_fails_before_its_tree_is_trusted(
    measured: tuple[dict[str, Any], Path], repository: Path, tmp_path: Path
) -> None:
    directory, digest, environment_digest = downloaded(measured, tmp_path)
    environment = canon.loads((directory / ra.ENVIRONMENT_FILE).read_bytes())
    environment["tree"] = "f" * 40
    (directory / ra.ENVIRONMENT_FILE).write_bytes(canon.dumps(environment))
    # Only the digest fails: a tree id from an unproven record is never checked out against.
    assert ra.verify(directory, digest, environment_digest, repository) == [
        f"the job log's environment digest {environment_digest} is not "
        f"{ra.ENVIRONMENT_FILE}'s {sha256_of(directory / ra.ENVIRONMENT_FILE)}"
    ]


def test_a_tampered_report_fails_every_way_it_can(
    measured: tuple[dict[str, Any], Path], repository: Path, tmp_path: Path
) -> None:
    directory, _digest, environment_digest = downloaded(measured, tmp_path)
    report = canon.loads((directory / ra.REPORT_FILE).read_bytes())
    report["inputs"]["database"]["collation_version"] = "0"
    data = canon.dumps(report)
    (directory / ra.REPORT_FILE).write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    (directory / ra.DIGEST_FILE).write_bytes(f"{digest}  report.json\n".encode())
    problems = ra.verify(directory, digest, environment_digest, repository)
    assert "fingerprint is not SHA-256(canonical(inputs))" in problems
    assert "component database is not SHA-256(canonical(inputs.database))" in problems


def test_a_report_that_is_not_canonical_fails(
    measured: tuple[dict[str, Any], Path], repository: Path, tmp_path: Path
) -> None:
    directory, _digest, environment_digest = downloaded(measured, tmp_path)
    data = json.dumps(json.loads((directory / ra.REPORT_FILE).read_bytes()), indent=1).encode()
    (directory / ra.REPORT_FILE).write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    (directory / ra.DIGEST_FILE).write_bytes(f"{digest}  report.json\n".encode())
    problems = ra.verify(directory, digest, environment_digest, repository)
    assert len(problems) == 1 and problems[0].startswith("not canonical")


def test_repository_groups_must_recompute_at_the_runs_tree(
    measured: tuple[dict[str, Any], Path], repository: Path, tmp_path: Path
) -> None:
    directory, digest, environment_digest = downloaded(measured, tmp_path)
    run_tree = ff.git(repository, "rev-parse", "HEAD^{tree}")
    moved = tmp_path / "moved"
    shutil.copytree(repository, moved)
    (moved / "pyproject.toml").write_bytes((moved / "pyproject.toml").read_bytes() + b"\n")
    ff.git(moved, "-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-qam", "x")
    moved_head = ff.git(moved, "rev-parse", "HEAD")
    moved_tree = ff.git(moved, "rev-parse", "HEAD^{tree}")
    assert ra.verify(directory, digest, environment_digest, moved) == [
        f"check out a commit whose tree is the run's {run_tree}; HEAD {moved_head} has "
        f"tree {moved_tree}"
    ]
    # A record forged to name the moved tree (with a log digest to match) still fails: the
    # sources recompute from the moved tree's blobs, not the report's.
    environment = canon.loads((directory / ra.ENVIRONMENT_FILE).read_bytes())
    environment["tree"] = moved_tree
    (directory / ra.ENVIRONMENT_FILE).write_bytes(canon.dumps(environment))
    forged = sha256_of(directory / ra.ENVIRONMENT_FILE)
    assert ra.verify(directory, digest, forged, moved) == [
        f"sources does not recompute at {moved_head}"
    ]


def test_another_commit_with_the_runs_exact_tree_verifies(
    measured: tuple[dict[str, Any], Path], repository: Path, tmp_path: Path
) -> None:
    """A pull request's merge commit: a different commit id, the measured tree exactly."""
    directory, digest, environment_digest = downloaded(measured, tmp_path)
    merge = tmp_path / "merge"
    shutil.copytree(repository, merge)
    ff.git(merge, "commit", "-q", "--allow-empty", "-m", "Merge into main")
    environment = canon.loads((directory / ra.ENVIRONMENT_FILE).read_bytes())
    assert ff.git(merge, "rev-parse", "HEAD") != environment["commit"]
    assert ff.git(merge, "rev-parse", "HEAD^{tree}") == environment["tree"]
    assert ra.verify(directory, digest, environment_digest, merge) == []


def test_an_environment_record_without_a_tree_id_fails(
    measured: tuple[dict[str, Any], Path], repository: Path, tmp_path: Path
) -> None:
    directory, digest, _environment_digest = downloaded(measured, tmp_path)
    environment = canon.loads((directory / ra.ENVIRONMENT_FILE).read_bytes())
    for tree in (None, "HEAD", "a" * 39):
        if tree is None:
            environment.pop("tree", None)
        else:
            environment["tree"] = tree
        (directory / ra.ENVIRONMENT_FILE).write_bytes(canon.dumps(environment))
        forged = sha256_of(directory / ra.ENVIRONMENT_FILE)
        assert ra.verify(directory, digest, forged, repository) == [
            f"{ra.ENVIRONMENT_FILE} records no tree id: {tree!r}"
        ]


def test_a_refused_run_has_nothing_to_pin(tmp_path: Path) -> None:
    for name in ra.REFUSAL_SET:
        (tmp_path / name).write_bytes(b"{}")
    # Reported as refused, not as a run missing its report, environment and digest.
    assert ra.verify(tmp_path, "0" * 64, "0" * 64) == ["the run refused; there is no report to pin"]
    assert ra.verify(tmp_path / "absent", "0" * 64, "0" * 64) == [
        f"{tmp_path / 'absent'} is not a directory"
    ]


@pytest.mark.parametrize(
    "extra", [ra.REPORT_FILE, ra.ENVIRONMENT_FILE, ra.DIGEST_FILE, ".refusal.json.partial"]
)
def test_a_refused_run_holds_exactly_docker_json_and_refusal_json(
    tmp_path: Path, extra: str
) -> None:
    for name in (*ra.REFUSAL_SET, extra):
        (tmp_path / name).write_bytes(b"{}")
    found = sorted({*ra.REFUSAL_SET, extra})
    assert ra.verify(tmp_path, "0" * 64, "0" * 64) == [
        "the run refused; there is no report to pin",
        f"a refused run holds exactly {sorted(ra.REFUSAL_SET)}; found {found}",
    ]


def test_a_real_refusal_leaves_exactly_the_refusal_set(
    dataset: RetrievalDataset, tmp_path: Path
) -> None:
    repo = ff.make_repository(tmp_path / "dirty")
    (repo / "alembic.ini").write_bytes(b"# edited\n")
    out = tmp_path / "out"
    with pytest.raises(MeasurementRefused):
        measure(dataset, repo, out)
    assert {p.name for p in out.iterdir()} == ra.REFUSAL_SET
    assert ra.verify(out, "0" * 64, "0" * 64) == ["the run refused; there is no report to pin"]


def test_the_command_line_reports_and_exits(
    measured: tuple[dict[str, Any], Path],
    repository: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    directory, digest, environment_digest = downloaded(measured, tmp_path)
    arguments = ["verify", str(directory), "--repository", str(repository)]
    environment = ["--environment-log-digest", environment_digest]
    assert ra.main([*arguments, *environment, "--log-digest", digest]) == 0
    assert capsys.readouterr().out.startswith("OK")
    assert ra.main([*arguments, *environment, "--log-digest", "0" * 64]) == 1
    assert capsys.readouterr().out.startswith("FAIL")
    # The environment digest is required, never defaulted.
    with pytest.raises(SystemExit) as caught:
        ra.main([*arguments, "--log-digest", digest])
    assert caught.value.code == 2
    assert "--environment-log-digest" in capsys.readouterr().err


# --- the workflow (§12.6 "Artifacts") and .gitattributes (§12.6 "Pinning" step 4) -----------------

WORKFLOW = yaml.safe_load((ROOT / fp.CI_FILE).read_text(encoding="utf-8"))
JOB = WORKFLOW["jobs"][fp.CI_JOB]
STEPS = JOB["steps"]
NAMES = [step["name"] for step in STEPS]
DOCKER_STEP = "Record the PostgreSQL container's Docker metadata"
PRINT_STEP = "Print the retrieval evaluation artifacts' SHA-256"
UPLOAD_STEP = "Upload the retrieval evaluation artifacts"


def step(name: str) -> dict[str, Any]:
    return STEPS[NAMES.index(name)]


def test_the_workflow_variable_equals_the_service_image() -> None:
    assert JOB["env"][fp.IMAGE_VARIABLE] == JOB["services"]["postgres"]["image"] == ff.IMAGE


def test_the_docker_metadata_is_written_in_shard_1_before_the_tests() -> None:
    docker = step(DOCKER_STEP)
    assert NAMES.index(DOCKER_STEP) < NAMES.index("pytest")
    assert NAMES.index(DOCKER_STEP) > NAMES.index("Verify the migration chain")
    assert docker["if"] == "matrix.shard == 1"
    assert docker["env"]["POSTGRES_CONTAINER"] == "${{ job.services.postgres.id }}"
    run = docker["run"]
    assert 'out="$RUNNER_TEMP/retrieval-eval"' in run
    assert 'echo "RETRIEVAL_EVAL_OUT=$out" >> "$GITHUB_ENV"' in run
    assert 'RETRIEVAL_EVAL_DOCKER_FILE="$out/docker.json"' in run
    script = run.split("<<'PY'\n", 1)[1].rsplit("PY\n", 1)[0]
    compile(script, "docker-metadata", "exec")
    for needed in ('"inspect"', '"image", "inspect"', '"info", "--format", "{{json .}}"'):
        assert needed in script
    assert '{"container": container, "image": image, "info": info}' in script


#: actions/upload-artifact v4.6.2, verified with `gh api repos/actions/upload-artifact/git/ref/
#: tags/v4.6.2` on 2026-11-13.
UPLOAD_ARTIFACT_SHA = "ea165f8d65b6e75b540449e92b4886f43607fa02"


def test_every_artifact_is_printed_and_uploaded_whatever_the_tests_did() -> None:
    printing, upload = step(PRINT_STEP), step(UPLOAD_STEP)
    assert NAMES.index("pytest") < NAMES.index(PRINT_STEP) < NAMES.index(UPLOAD_STEP)
    for item in (printing, upload):
        assert item["if"] == "always() && matrix.shard == 1"
    assert "sha256sum" in printing["run"] and '"$RETRIEVAL_EVAL_OUT"' in printing["run"]
    action, _, sha = upload["uses"].partition("@")
    assert action == "actions/upload-artifact"
    # The commit v4.6.2 tags, read from GitHub (refs/tags/v4.6.2, a lightweight tag) on
    # 2026-11-13; a 40-hex pattern alone let an unverified SHA through.
    assert sha == UPLOAD_ARTIFACT_SHA, "pinned by v4.6.2's verified commit SHA"
    assert upload["with"] == {
        "name": "retrieval-eval-${{ github.run_id }}-${{ github.run_attempt }}",
        "path": "${{ env.RETRIEVAL_EVAL_OUT }}",
        "if-no-files-found": "error",
    }


def test_the_upload_pin_carries_its_version_comment() -> None:
    # Since A3 and A9 the workflow uploads three artifacts (the retrieval report, the SBOMs
    # and the responsive-audit report). Every use is v4.6.2's verified commit, with its
    # version comment; none may pin anything else.
    text = (ROOT / fp.CI_FILE).read_text(encoding="utf-8")
    uses = re.findall(r"^\s*uses: (actions/upload-artifact@.*)$", text, re.MULTILINE)
    assert text.count("actions/upload-artifact@") == len(uses) == 3
    assert set(uses) == {f"actions/upload-artifact@{UPLOAD_ARTIFACT_SHA} # v4.6.2"}


def test_the_measurement_reads_the_files_the_workflow_writes() -> None:
    assert ra.OUT_VARIABLE == "RETRIEVAL_EVAL_OUT"
    assert ra.DOCKER_FILE == "docker.json"
    assert ra.OUTPUTS == ("report.json", "environment.json", "report.json.sha256", "refusal.json")
    assert sorted(ra.SUCCESS_SET) == [
        "docker.json",
        "environment.json",
        "report.json",
        "report.json.sha256",
    ]
    assert sorted(ra.REFUSAL_SET) == ["docker.json", "refusal.json"]


def test_the_pinned_paths_are_exact() -> None:
    assert ra.PINNED_REPORT == "tests/evaluation/retrieval_report_v2.json"
    assert ra.PINNED_ENVIRONMENT == "tests/evaluation/retrieval_report_v2.environment.json"
    # Neither is a fingerprinted source: the pin commit changes no fingerprinted file (§12.6).
    assert not {ra.PINNED_REPORT, ra.PINNED_ENVIRONMENT} & set(fp.SOURCE_FILES)


def test_both_pinned_files_are_marked_minus_text_in_gitattributes() -> None:
    lines = (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert f"{ra.PINNED_REPORT} -text" in lines
    assert f"{ra.PINNED_ENVIRONMENT} -text" in lines


def test_git_reads_both_pinned_files_as_minus_text() -> None:
    done = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "check-attr",
            "text",
            "--",
            ra.PINNED_REPORT,
            ra.PINNED_ENVIRONMENT,
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    assert done.stdout.splitlines() == [
        f"{ra.PINNED_REPORT}: text: unset",
        f"{ra.PINNED_ENVIRONMENT}: text: unset",
    ]


def test_the_workflow_and_gitattributes_are_fingerprinted() -> None:
    assert {fp.CI_FILE, ".gitattributes"} <= set(fp.SOURCE_FILES)
