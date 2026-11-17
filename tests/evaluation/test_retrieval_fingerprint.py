"""R2.10C -- decision F §12.1-12.3, §12.6-12.7: the fingerprint's groups, manifest and refusals.

Offline. Git runs only on throwaway repositories under `tmp_path`; nothing touches this repository's
index or history.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.evaluation import fingerprint_fixtures as ff
from tests.evaluation import retrieval_canonical as canon
from tests.evaluation import retrieval_fingerprint as fp
from tests.evaluation import retrieval_numerics as rn
from tests.evaluation import retrieval_review as rr
from tests.evaluation import retrieval_signoff as so
from tests.evaluation import retrieval_v2 as v2
from tests.evaluation.retrieval_canonical import Binary64
from tests.evaluation.retrieval_fingerprint import MeasurementRefused
from tests.evaluation.retrieval_schema import RetrievalDataset

ROOT = fp.REPOSITORY_ROOT
DATASET_SHA256 = "7f35b8a473c73f6d318e48ce5cc2667294f0d50816c511cfaf7851585c617031"


@pytest.fixture(scope="module")
def dataset() -> RetrievalDataset:
    return v2.load()


def refused(reason: str) -> Any:
    return pytest.raises(MeasurementRefused, match=f"^{reason}: ")


# --- the groups and their declared types ----------------------------------------------------------


def test_the_groups_are_those_of_12_3_and_12_4_in_order() -> None:
    assert fp.GROUPS == (
        "engine",
        "database",
        "text_search",
        "document_state",
        "chunk_state",
        "query_parsing",
        "schema",
        "sources",
        "python",
        "dataset",
        "metrics",
    )
    assert set(fp.REPOSITORY_GROUPS) == {"sources", "dataset", "metrics"}


def test_one_refusal_reason_per_12_7_bullet() -> None:
    assert len(fp.REASONS) == 9 == len(set(fp.REASONS))


def sample_inputs(dataset: RetrievalDataset) -> dict[str, Any]:
    """Complete inputs: database groups from the fake server, the others computed for real."""
    from tests.evaluation import retrieval_probes as rp

    db = ff.database(dataset)
    state = rp.read_state(db, db.ingested.uploads)
    number, version, extensions = rp.server(db)
    docker = fp.docker_identity(ff.docker_metadata(), fp.DeclaredImage("postgres:18.6", ff.DIGEST))
    return {
        "engine": fp.engine_group(docker, number, version, extensions),
        "database": rp.database_group(db),
        "text_search": rp.text_search_group(db, ["english", "greek"]),
        "document_state": state.documents,
        "chunk_state": state.chunks,
        "query_parsing": rp.query_parsing_group(db, dataset),
        "schema": rp.schema_group(db),
        "sources": {"alembic.ini": {"blob": "a" * 40, "sha256": "b" * 64}},
        "python": {
            "implementation": "cpython",
            "version": "3.14.7",
            "build": "3.14.7 (main) [GCC]",
            "float_format": {"mant_dig": 53, "radix": 2, "max_exp": 1024, "repr_style": "short"},
            "decimal": {"module": "1.70", "libmpdec": "4.0.1"},
            "installed_set_sha256": "c" * 64,
        },
        "dataset": fp.dataset_group(dataset),
        "metrics": fp.metrics_group(),
    }


SIGNOFF = {
    "file": "tests/evaluation/datasets/knowledge_retrieval_v2.signoff.json",
    "sha256": "d" * 64,
    "decision": "approved",
    "dataset_sha256": DATASET_SHA256,
}


def test_complete_inputs_have_their_declared_types(dataset: RetrievalDataset) -> None:
    fp.check_inputs(sample_inputs(dataset))


def test_the_fingerprint_is_sha256_of_the_canonical_inputs(dataset: RetrievalDataset) -> None:
    inputs = sample_inputs(dataset)
    manifest = fp.build_manifest(inputs, SIGNOFF)
    assert manifest["fingerprint"] == hashlib.sha256(canon.dumps(inputs)).hexdigest()
    assert manifest["components"] == {
        group: hashlib.sha256(canon.dumps(inputs[group])).hexdigest() for group in fp.GROUPS
    }
    assert manifest["manifest"] == fp.FINGERPRINT_VERSION
    assert fp.manifest_problems(canon.loads(canon.dumps(manifest))) == []


def test_the_sign_off_is_beside_the_fingerprint_not_part_of_it(dataset: RetrievalDataset) -> None:
    inputs = sample_inputs(dataset)
    first = fp.build_manifest(inputs, SIGNOFF)
    second = fp.build_manifest(inputs, {**SIGNOFF, "sha256": "e" * 64})
    assert first["fingerprint"] == second["fingerprint"]
    assert first["signoff"] != second["signoff"]


@pytest.mark.parametrize("group", fp.GROUPS)
def test_tampering_with_one_group_names_that_group(dataset: RetrievalDataset, group: str) -> None:
    manifest = canon.loads(canon.dumps(fp.build_manifest(sample_inputs(dataset), SIGNOFF)))
    tampered = copy.deepcopy(manifest)
    tampered["components"][group] = "0" * 64
    assert fp.manifest_problems(tampered) == [
        f"component {group} is not SHA-256(canonical(inputs.{group}))"
    ]
    tampered = copy.deepcopy(manifest)
    tampered["fingerprint"] = "0" * 64
    assert fp.manifest_problems(tampered) == ["fingerprint is not SHA-256(canonical(inputs))"]


def test_the_comparison_is_fingerprint_first_then_results(dataset: RetrievalDataset) -> None:
    inputs = sample_inputs(dataset)
    pinned = {**fp.build_manifest(inputs, SIGNOFF), "results": {"a": 1}}
    assert fp.compare(pinned, copy.deepcopy(pinned)) is None
    same_inputs = {**copy.deepcopy(pinned), "results": {"a": 2}}
    assert fp.compare(pinned, same_inputs) == "results changed with unchanged inputs"
    changed = copy.deepcopy(inputs)
    changed["database"]["collation_version"] = "153.121"
    measured = {**fp.build_manifest(changed, SIGNOFF), "results": {"a": 2}}
    assert fp.compare(pinned, measured) == "inputs changed: database"


def test_null_appears_only_where_12_2_allows_it(dataset: RetrievalDataset) -> None:
    inputs = sample_inputs(dataset)
    assert any(row["supersedes"] is None for row in inputs["document_state"])
    assert any(row["supersedes"] is not None for row in inputs["document_state"])
    assert inputs["database"]["locale_provider"] == "libc"
    assert inputs["database"]["locale"] is None
    assert inputs["text_search"]["dictionaries"]["pg_catalog.simple"]["init_options"] is None
    assert inputs["engine"]["platform"]["variant"] is None
    fp.build_manifest(inputs, SIGNOFF)
    for group, path in (
        ("engine", ("platform", "os")),
        ("database", ("collate",)),
        ("database", ("collation_version",)),
        ("engine", ("version",)),
        ("schema", ("alembic_head",)),
        ("python", ("decimal", "libmpdec")),
        ("text_search", ("dictionaries", "pg_catalog.english_stem", "init_options")),
        ("text_search", ("dictionaries", "pg_catalog.simple", "template")),
    ):
        broken = copy.deepcopy(inputs)
        target = broken[group]
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = None
        with refused("unmeasurable"):
            fp.build_manifest(broken, SIGNOFF)
    broken = copy.deepcopy(inputs)
    broken["document_state"][0]["title"] = None
    with refused("unmeasurable"):
        fp.build_manifest(broken, SIGNOFF)


@pytest.mark.parametrize(
    ("provider", "locale"), [("libc", "en_US.utf8"), ("builtin", None), ("icu", None)]
)
def test_the_manifest_holds_the_locale_to_its_provider(
    dataset: RetrievalDataset, provider: str, locale: str | None
) -> None:
    broken = sample_inputs(dataset)
    broken["database"].update(locale_provider=provider, locale=locale)
    with refused("unmeasurable"):
        fp.build_manifest(broken, SIGNOFF)
    valid = sample_inputs(dataset)
    manifest = canon.loads(canon.dumps(fp.build_manifest(valid, SIGNOFF)))
    manifest["inputs"]["database"].update(locale_provider=provider, locale=locale)
    assert any("database.locale" in problem for problem in fp.manifest_problems(manifest))


@pytest.mark.parametrize(
    ("key", "change"),
    [
        ("pg_catalog.english_stem", {"init_options": None}),
        ("pg_catalog.greek_stem", {"init_options": None}),
        ("pg_catalog.simple", {"init_options": ""}),
        ("pg_catalog.simple", {"schema": "public"}),
        ("pg_catalog.simple", {"name": "simple_copy"}),
    ],
)
def test_the_manifest_allows_null_init_options_only_for_pg_catalog_simple(
    dataset: RetrievalDataset, key: str, change: dict[str, Any]
) -> None:
    broken = sample_inputs(dataset)
    broken["text_search"]["dictionaries"][key].update(change)
    with refused("unmeasurable"):
        fp.build_manifest(broken, SIGNOFF)


def test_the_manifest_accepts_a_set_locale_under_builtin_and_icu(dataset: RetrievalDataset) -> None:
    for provider, locale in (("builtin", "C.UTF-8"), ("icu", "en-US")):
        inputs = sample_inputs(dataset)
        inputs["database"].update(locale_provider=provider, locale=locale)
        fp.build_manifest(inputs, SIGNOFF)


def test_a_permitted_null_is_a_typed_null_the_fingerprint_covers(dataset: RetrievalDataset) -> None:
    inputs = sample_inputs(dataset)
    text = canon.dumps(inputs).decode("utf-8")
    assert '"locale":null' in text and '"init_options":null' in text
    assert '"variant":null' in text
    pinned = {**fp.build_manifest(inputs, SIGNOFF), "results": {}}
    located = copy.deepcopy(inputs)
    located["database"].update(locale_provider="builtin", locale="C.UTF-8")
    optioned = copy.deepcopy(inputs)
    optioned["text_search"]["dictionaries"]["pg_catalog.simple"]["init_options"] = "accept = 'true'"
    varied = copy.deepcopy(inputs)
    varied["engine"]["platform"]["variant"] = "v8"
    for changed, group in (
        (located, "database"),
        (optioned, "text_search"),
        (varied, "engine"),
    ):
        measured = {**fp.build_manifest(changed, SIGNOFF), "results": {}}
        assert measured["fingerprint"] != pinned["fingerprint"]
        assert measured["components"][group] != pinned["components"][group]
        assert fp.compare(pinned, measured) == f"inputs changed: {group}"
    alone = copy.deepcopy(inputs["database"])
    alone["locale"] = "en_US.utf8"
    assert canon.sha256(alone) != canon.sha256(inputs["database"])


@pytest.mark.parametrize("variant", ["", 8, True, ["v8"]])
def test_the_manifest_holds_the_variant_to_null_or_a_nonempty_string(
    dataset: RetrievalDataset, variant: object
) -> None:
    broken = sample_inputs(dataset)
    broken["engine"]["platform"]["variant"] = variant
    with refused("unmeasurable"):
        fp.build_manifest(broken, SIGNOFF)
    manifest = canon.loads(canon.dumps(fp.build_manifest(sample_inputs(dataset), SIGNOFF)))
    manifest["inputs"]["engine"]["platform"]["variant"] = variant
    assert any("variant" in problem for problem in fp.manifest_problems(manifest))


def test_the_manifest_accepts_a_null_or_a_named_variant(dataset: RetrievalDataset) -> None:
    for variant in (None, "v8"):
        inputs = sample_inputs(dataset)
        inputs["engine"]["platform"]["variant"] = variant
        manifest = fp.build_manifest(inputs, SIGNOFF)
        assert fp.manifest_problems(canon.loads(canon.dumps(manifest))) == []


# --- empty strings: a value only where PostgreSQL gives one (§12.3) -------------------------------


def test_an_empty_tsquery_is_a_value(dataset: RetrievalDataset) -> None:
    inputs = sample_inputs(dataset)
    inputs["query_parsing"][0]["tsquery"] = ""  # a query of stop words alone parses to ''
    manifest = fp.build_manifest(inputs, SIGNOFF)
    assert fp.manifest_problems(canon.loads(canon.dumps(manifest))) == []


def test_an_empty_template_lexize_refuses(dataset: RetrievalDataset) -> None:
    inputs = sample_inputs(dataset)
    for template in inputs["text_search"]["templates"].values():
        template["lexize"] = ""
    with refused("unmeasurable"):
        fp.build_manifest(inputs, SIGNOFF)


def test_an_empty_token_type_description_refuses(dataset: RetrievalDataset) -> None:
    inputs = sample_inputs(dataset)
    for configuration in inputs["text_search"]["configurations"].values():
        configuration["parser"]["token_types"][0]["description"] = ""
    with refused("unmeasurable"):
        fp.build_manifest(inputs, SIGNOFF)


@pytest.mark.parametrize(
    ("group", "keys"),
    [
        ("document_state", ("hotel", "doc_id")),
        ("chunk_state", ("doc_id", "ordinal")),
        ("query_parsing", ("query_id", "configuration")),
    ],
)
def test_ordered_groups_must_keep_their_declared_order(
    dataset: RetrievalDataset, group: str, keys: tuple[str, ...]
) -> None:
    inputs = sample_inputs(dataset)
    reordered = copy.deepcopy(inputs)
    reordered[group] = list(reversed(reordered[group]))
    with refused("unmeasurable"):
        fp.build_manifest(reordered, SIGNOFF)
    repeated = copy.deepcopy(inputs)
    repeated[group] = [repeated[group][0], *repeated[group]]
    with refused("unmeasurable"):
        fp.build_manifest(repeated, SIGNOFF)


def test_a_float_object_never_enters_the_inputs(dataset: RetrievalDataset) -> None:
    inputs = sample_inputs(dataset)
    inputs["metrics"]["configuration"]["fixed"]["z"] = rn.LOG2_3
    with refused("unmeasurable"):
        fp.build_manifest(inputs, SIGNOFF)
    with pytest.raises(canon.CanonicalError, match="float"):
        canon.dumps(inputs)


def test_a_missing_or_extra_group_is_refused(dataset: RetrievalDataset) -> None:
    inputs = sample_inputs(dataset)
    with refused("unmeasurable"):
        fp.build_manifest({k: v for k, v in inputs.items() if k != "schema"}, SIGNOFF)
    with refused("unmeasurable"):
        fp.build_manifest({**inputs, "extra": {}}, SIGNOFF)


# --- metrics and dataset --------------------------------------------------------------------------


def test_the_metrics_group_writes_every_binary64_as_float_hex() -> None:
    group = canon.loads(canon.dumps(fp.metrics_group()))
    configuration = group["configuration"]
    assert configuration["ndcg_gains"] == {
        "0": "0x0.0p+0",
        "1": "0x1.0000000000000p+0",
        "2": "0x1.8000000000000p+1",
    }
    assert configuration["fixed"]["z"] == (1.959963984540054).hex()
    assert configuration["mrr_min_grade"] == 2
    assert configuration["fixed"]["mean_interval"] == {
        "method": "percentile bootstrap, queries resampled with replacement",
        "resamples": 10000,
        "seed": 20261108,
        "min_n": 10,
        "percentiles": "2.5 and 97.5, nearest rank",
    }
    assert group["discount_table"] == {
        "1": "0x1.0000000000000p+0",
        "2": rn.LOG2_3.hex(),
        "3": "0x1.0000000000000p+1",
        "4": rn.LOG2_5.hex(),
        "5": rn.LOG2_6.hex(),
    }
    assert group["numeric_contract"] == "retrieval_numerics_v1"
    assert group["metrics_version"] == "knowledge_retrieval_metrics_v1"


def test_a_numeric_certificate_failure_refuses_the_metrics_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken() -> None:
        raise rn.NumericVerificationError("certificate failed")

    monkeypatch.setattr(fp, "discount_table", broken)
    with refused("numeric_certificate"):
        fp.metrics_group()


def test_the_dataset_group_is_the_signed_dataset(dataset: RetrievalDataset) -> None:
    assert fp.dataset_group(dataset) == {
        "identity": "knowledge_retrieval_v2",
        "schema": "knowledge_retrieval_schema_v2",
        "sha256": DATASET_SHA256,
    }


def test_coerce_turns_declared_binary64_numbers_into_binary64_and_nothing_else() -> None:
    spec = canon.OBJ(gain=canon.B64(), count=canon.INT())
    assert fp.coerce({"gain": 3, "count": 3}, spec) == {"gain": Binary64(3.0), "count": 3}
    with pytest.raises(canon.CanonicalError):
        fp.coerce({"gain": True, "count": 3}, spec)
    with pytest.raises(canon.CanonicalError):
        canon.check(fp.coerce({"gain": 3, "count": 3.0}, spec), spec)


# --- the sources (§12.3, §12.6) -------------------------------------------------------------------

#: §12.3's list, verbatim.
SPEC_SOURCES = (
    ".github/workflows/ci.yml",
    ".gitattributes",
    "pyproject.toml",
    "alembic.ini",
    "backend/app/main.py",
    "backend/app/api/deps.py",
    "backend/app/api/v1/endpoints/knowledge.py",
    "backend/app/services/knowledge.py",
    "backend/app/services/scope.py",
    "backend/app/repositories/hotel.py",
    "backend/app/repositories/knowledge.py",
    "backend/app/knowledge/chunking.py",
    "backend/app/schemas/knowledge.py",
    "backend/app/models/knowledge.py",
    "backend/app/models/enums.py",
    "database/migrations/env.py",
    "tests/conftest.py",
    "tests/integration/conftest.py",
    "tests/evaluation/retrieval.py",
    "tests/evaluation/retrieval_schema.py",
    "tests/evaluation/retrieval_metrics.py",
    "tests/evaluation/datasets/knowledge_retrieval_v2.json",
    "backend/requirements-lock.txt",
    "backend/requirements-dev-lock.txt",
)


#: The C.4 harness (§12.3): the measurement's own modules and those it loads its inputs through.
HARNESS = (
    "tests/evaluation/retrieval_canonical.py",
    "tests/evaluation/retrieval_fingerprint.py",
    "tests/evaluation/retrieval_probes.py",
    "tests/evaluation/retrieval_artifacts.py",
    "tests/evaluation/retrieval_numerics.py",
    "tests/evaluation/retrieval_review.py",
    "tests/evaluation/retrieval_signoff.py",
    "tests/evaluation/retrieval_v2.py",
)
#: §12.3's package `__init__.py` files.
INITS = (
    "tests/__init__.py",
    "tests/evaluation/__init__.py",
    "tests/integration/__init__.py",
    "backend/app/__init__.py",
    "backend/app/api/__init__.py",
    "backend/app/api/v1/__init__.py",
    "backend/app/api/v1/endpoints/__init__.py",
    "backend/app/services/__init__.py",
    "backend/app/repositories/__init__.py",
    "backend/app/knowledge/__init__.py",
    "backend/app/schemas/__init__.py",
    "backend/app/models/__init__.py",
    "backend/app/db/__init__.py",
)
#: §12.3's exclusions, each by reason.
EXCLUDED = (
    "database/migrations/script.py.mako",
    "tests/db_safety.py",
    "backend/app/core/__init__.py",
    "backend/app/core/config.py",
    "backend/app/core/client_address.py",
    "backend/app/core/errors.py",
    "backend/app/core/request_id.py",
    "backend/app/core/security_headers.py",
    "backend/app/db/base.py",
    "backend/app/db/session.py",
    *(
        f"backend/app/models/{name}.py"
        for name in (
            "audit", "booking", "copilot_conversation", "demand_observation", "finance",
            "guest", "hotel", "llm_invocation", "membership", "metrics", "payment",
            "platform", "prediction", "review", "room", "user",
        )
    ),
    *(
        f"backend/app/schemas/{name}.py"
        for name in (
            "amenity", "analytics", "auth", "booking", "common", "finance", "guest",
            "health", "hotel", "intelligence", "meta", "payment", "review", "room",
            "room_type",
        )
    ),
)  # fmt: skip


def test_the_source_inventory_is_exactly_12_3s() -> None:
    assert fp.HARNESS_SOURCES == HARNESS
    assert fp.PACKAGE_INITS == INITS
    assert sorted(fp.SOURCE_FILES) == sorted((*SPEC_SOURCES, *HARNESS, fp.INTEGRATION_TEST, *INITS))
    assert len(fp.SOURCE_FILES) == 46


def test_the_exclusions_are_exactly_12_3s_each_with_a_reason() -> None:
    assert sorted(fp.EXCLUDED_SOURCES) == sorted(EXCLUDED)
    assert len(fp.EXCLUDED_SOURCES) == 41
    assert all(reason.strip() for reason in fp.EXCLUDED_SOURCES.values())
    # §12.3 once named this path; the declarative base lives in app/db/base.py.
    assert "backend/app/models/base.py" not in fp.EXCLUDED_SOURCES


def test_every_excluded_source_exists_in_this_tree() -> None:
    assert [path for path in fp.EXCLUDED_SOURCES if not (ROOT / path).is_file()] == []


def test_every_excluded_source_is_tracked_at_head() -> None:
    git = fp.git_runner(ROOT)
    tracked = fp.tracked_tree(git, fp.measured_commit(git), list(fp.EXCLUDED_SOURCES))
    assert sorted(tracked) == sorted(fp.EXCLUDED_SOURCES)


_CLOSURE = """
import json, pathlib, sys
import tests.evaluation.retrieval_artifacts
root = pathlib.Path.cwd().resolve()
found = set()
for module in list(sys.modules.values()):
    file = getattr(module, "__file__", None)
    if file and pathlib.Path(file).resolve().is_relative_to(root):
        found.add(pathlib.Path(file).resolve().relative_to(root).as_posix())
sys.stdout.write(json.dumps(sorted(found)))
"""


def harness_import_closure() -> list[str]:
    """Every repository file a fresh interpreter loads to import the measurement path."""
    done = subprocess.run(
        [sys.executable, "-B", "-c", _CLOSURE],
        capture_output=True,
        check=True,
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": f"backend{os.pathsep}."},
    )
    loaded = json.loads(done.stdout)
    # A virtual environment inside the checkout is not the repository's code.
    return [path for path in loaded if not set(path.split("/")) & fp.EXEMPT_DIRECTORIES]


def test_the_import_closure_is_fingerprinted_or_excluded_by_reason() -> None:
    loaded = harness_import_closure()
    assert set(HARNESS) <= set(loaded)
    assert "backend/app/knowledge/chunking.py" in loaded
    assert fp.unaccounted_modules(loaded) == []
    # Every loaded package's __init__ is fingerprinted or excluded with its package.
    assert {path for path in loaded if path.endswith("/__init__.py")} <= set(INITS) | set(EXCLUDED)


def test_a_new_unlisted_repository_module_is_reported() -> None:
    loaded = [
        "tests/evaluation/retrieval_v2.py",
        "backend/app/db/base.py",
        "backend/app/core/new_helper.py",
        "tests/evaluation/new_scorer.py",
    ]
    assert fp.unaccounted_modules(loaded) == [
        "backend/app/core/new_helper.py",
        "tests/evaluation/new_scorer.py",
    ]


def test_every_source_of_12_3_is_fingerprinted_and_no_excluded_one() -> None:
    assert set(SPEC_SOURCES) <= set(fp.SOURCE_FILES)
    assert set(fp.HARNESS_SOURCES) <= set(fp.SOURCE_FILES)
    assert fp.INTEGRATION_TEST in fp.SOURCE_FILES
    assert len(set(fp.SOURCE_FILES)) == len(fp.SOURCE_FILES)
    assert not set(fp.EXCLUDED_SOURCES) & set(fp.SOURCE_FILES)
    assert not any(path.startswith(fp.MIGRATIONS_DIR) for path in fp.SOURCE_FILES)


def test_every_source_but_the_c4_integration_test_exists_in_this_tree() -> None:
    missing = [path for path in fp.SOURCE_FILES if not (ROOT / path).is_file()]
    assert missing == [fp.INTEGRATION_TEST]


@pytest.fixture(scope="module")
def repository(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return ff.make_repository(tmp_path_factory.mktemp("fingerprint") / "repo")


def test_sources_are_hashed_from_git_blobs_with_their_ids(repository: Path) -> None:
    git = fp.git_runner(repository)
    commit = fp.measured_commit(git)
    assert commit == ff.git(repository, "rev-parse", "HEAD")
    fp.require_clean(git)
    sources = fp.sources_group(git, commit, repository)
    canon.check(sources, fp.SOURCES_SPEC)
    versions = sorted(p.name for p in (ROOT / fp.MIGRATIONS_DIR).glob("*.py"))
    assert (
        sorted(path.rsplit("/", 1)[1] for path in sources if path.startswith(fp.MIGRATIONS_DIR))
        == versions
    )
    assert set(fp.SOURCE_FILES) <= set(sources)
    for path in ("alembic.ini", fp.CI_FILE, fp.INTEGRATION_TEST):
        blob = subprocess.run(
            ["git", "-C", str(repository), "cat-file", "blob", f"{commit}:{path}"],
            capture_output=True,
            check=True,
        ).stdout
        assert sources[path] == {
            "blob": ff.git(repository, "rev-parse", f"{commit}:{path}"),
            "sha256": hashlib.sha256(blob).hexdigest(),
        }


def test_blob_hashes_involve_no_line_ending_conversion(tmp_path: Path) -> None:
    repo = tmp_path / "crlf"
    repo.mkdir()
    ff.git(repo, "init", "-q")
    ff.git(repo, "config", "core.autocrlf", "false")
    (repo / ".gitattributes").write_bytes(b"* -text\n")
    (repo / "file.txt").write_bytes(b"a\r\nb\r\n")
    ff.git(repo, "add", "-A")
    ff.git(repo, "-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-qm", "x")
    git = fp.git_runner(repo)
    commit = fp.measured_commit(git)
    assert fp.blob_bytes(git, commit, "file.txt") == b"a\r\nb\r\n"


def test_a_dirty_tracked_tree_refuses(repository: Path, tmp_path: Path) -> None:
    repo = ff.make_repository(tmp_path / "dirty")
    git = fp.git_runner(repo)
    (repo / "alembic.ini").write_bytes((repo / "alembic.ini").read_bytes() + b"\n# edited\n")
    with refused("repository"):
        fp.require_clean(git)
    ff.git(repo, "add", "alembic.ini")
    with refused("repository"):
        fp.require_clean(git)


def test_an_untracked_file_outside_the_sources_does_not_dirty_the_tree(tmp_path: Path) -> None:
    repo = ff.make_repository(tmp_path / "untracked")
    (repo / "notes.txt").write_text("scratch", encoding="utf-8")
    fp.require_clean(fp.git_runner(repo))


def test_an_untracked_migration_refuses(tmp_path: Path) -> None:
    repo = ff.make_repository(tmp_path / "migration")
    (repo / fp.MIGRATIONS_DIR / "20991231_9999_stray.py").write_text("x = 1\n", encoding="utf-8")
    git = fp.git_runner(repo)
    with refused("repository"):
        fp.sources_group(git, fp.measured_commit(git), repo)


def test_an_excluded_source_missing_at_the_commit_refuses(tmp_path: Path) -> None:
    repo = ff.make_repository(tmp_path / "excluded")
    ff.git(repo, "rm", "-q", "backend/app/db/base.py")
    ff.git(repo, "commit", "-qm", "drop an excluded source")
    git = fp.git_runner(repo)
    with pytest.raises(MeasurementRefused, match="excluded sources missing") as caught:
        fp.sources_group(git, fp.measured_commit(git), repo)
    assert caught.value.reason == "repository"
    assert "backend/app/db/base.py" in caught.value.detail


def test_an_untracked_conftest_refuses(tmp_path: Path) -> None:
    repo = ff.make_repository(tmp_path / "conftest")
    (repo / "conftest.py").write_text("import os\n", encoding="utf-8")
    git = fp.git_runner(repo)
    fp.require_clean(git)  # untracked: the tracked tree itself is still clean
    with pytest.raises(MeasurementRefused, match="untracked files") as caught:
        fp.require_no_strays(git, repo)
    assert caught.value.reason == "repository"
    assert "conftest.py" in caught.value.detail


def test_an_untracked_file_of_any_kind_refuses(tmp_path: Path) -> None:
    repo = ff.make_repository(tmp_path / "notes")
    (repo / "tests" / "notes.txt").write_text("scratch", encoding="utf-8")
    with refused("repository"):
        fp.require_no_strays(fp.git_runner(repo), repo)


def ignore(repo: Path, *patterns: str) -> None:
    """Ignore *patterns* through `.git/info/exclude`, which changes no tracked file."""
    (repo / ".git" / "info").mkdir(parents=True, exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text(
        "".join(f"{pattern}\n" for pattern in patterns), encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("pattern", "path"),
    [
        ("*.pth", "evil.pth"),
        ("/site/", "site/nested/evil.pth"),
        ("/build/", "build/sitecustomize.py"),
        ("conftest_local.py", "tests/conftest_local.py"),
        ("/tests/integration/conftest.pyc", "tests/integration/conftest.pyc"),
        ("*.so", "backend/app/fast.cpython-314-x86_64-linux-gnu.so"),
    ],
)
def test_an_ignored_file_python_could_execute_refuses(
    tmp_path: Path, pattern: str, path: str
) -> None:
    repo = ff.make_repository(tmp_path / "ignored")
    ignore(repo, pattern)
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_bytes(b"import os\n")
    git = fp.git_runner(repo)
    fp.require_clean(git)
    with pytest.raises(MeasurementRefused, match="ignored files Python could execute") as caught:
        fp.require_no_strays(git, repo)
    assert caught.value.reason == "repository"
    assert path in caught.value.detail


def test_ignored_caches_environments_and_inert_files_pass(tmp_path: Path) -> None:
    repo = ff.make_repository(tmp_path / "caches")
    ignore(repo, ".venv/", "__pycache__/", "node_modules/", ".mypy_cache/", "*.log", "/out/")
    for path in (
        ".venv/lib/site-packages/distutils-precedence.pth",
        "tests/evaluation/__pycache__/retrieval_v2.cpython-314.pyc",
        "frontend/node_modules/gyp/pylib/gyp/__init__.py",
        ".mypy_cache/3.14/tests/evaluation/retrieval_v2.data.json",
        "run.log",
        "out/report.json",
    ):
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_bytes(b"x")
    fp.require_no_strays(fp.git_runner(repo), repo)


def test_a_missing_source_refuses(tmp_path: Path) -> None:
    repo = ff.make_repository(tmp_path / "missing", integration_test=False)
    git = fp.git_runner(repo)
    with pytest.raises(MeasurementRefused, match="sources missing") as caught:
        fp.sources_group(git, fp.measured_commit(git), repo)
    assert caught.value.reason == "repository"
    assert fp.INTEGRATION_TEST in caught.value.detail


def test_no_git_refuses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def absent(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", absent)
    with pytest.raises(MeasurementRefused, match="no Git"):
        fp.measured_commit(fp.git_runner(tmp_path))


def test_the_tree_id_is_the_commits(repository: Path) -> None:
    git = fp.git_runner(repository)
    commit = fp.measured_commit(git)
    assert fp.commit_tree(git, commit) == ff.git(repository, "rev-parse", "HEAD^{tree}")
    assert fp.commit_tree(git, commit) != commit


def test_a_directory_that_is_not_a_repository_refuses(tmp_path: Path) -> None:
    with refused("repository"):
        fp.measured_commit(fp.git_runner(tmp_path))


def test_module_names_follow_the_import_roots() -> None:
    assert fp.module_name("backend/app/main.py") == "app.main"
    assert fp.module_name("tests/evaluation/retrieval.py") == "tests.evaluation.retrieval"
    assert fp.module_name("database/migrations/env.py") is None
    assert fp.module_name("alembic.ini") is None
    # A package is imported by its own name, not as `<package>.__init__`.
    assert fp.module_name("backend/app/__init__.py") == "app"
    assert fp.module_name("backend/app/db/__init__.py") == "app.db"
    assert fp.module_name("tests/evaluation/__init__.py") == "tests.evaluation"


def test_the_harness_modules_are_imported_from_this_repository() -> None:
    fp.require_repository_imports(ROOT, fp.HARNESS_SOURCES)
    fp.require_repository_imports(ROOT, ["backend/app/knowledge/chunking.py"])
    fp.require_repository_imports(ROOT, fp.PACKAGE_INITS)


def test_a_module_imported_from_elsewhere_refuses(tmp_path: Path) -> None:
    elsewhere = str(tmp_path / "app" / "__init__.py")
    with refused("repository"):
        fp.require_repository_imports(ROOT, ["backend/app/__init__.py"], lambda _n: elsewhere)
    with refused("repository"):
        fp.require_repository_imports(
            ROOT, ["tests/evaluation/retrieval.py"], origin=lambda _n: str(tmp_path / "x.py")
        )
    with refused("repository"):
        fp.require_repository_imports(ROOT, ["tests/evaluation/retrieval.py"], lambda _n: None)


# --- the declared image and the Docker identity (§12.3 engine) ------------------------------------

CI_BYTES = (ROOT / fp.CI_FILE).read_bytes()


def test_the_workflow_declares_the_image_and_the_variable_matches() -> None:
    image = fp.declared_image(CI_BYTES, ff.IMAGE)
    assert (image.reference, image.digest) == ("postgres:18.6", ff.DIGEST)


@pytest.mark.parametrize(
    "variable", [None, "", "postgres:18.6", "postgres:18.6@sha256:" + "0" * 64, ff.IMAGE + " "]
)
def test_a_missing_or_different_variable_refuses(variable: str | None) -> None:
    with refused("image_identity"):
        fp.declared_image(CI_BYTES, variable)


def test_a_workflow_whose_two_declarations_differ_refuses() -> None:
    changed = CI_BYTES.replace(
        f"RETRIEVAL_EVAL_POSTGRES_IMAGE: {ff.IMAGE}".encode(),
        b"RETRIEVAL_EVAL_POSTGRES_IMAGE: postgres:18.6@sha256:" + b"0" * 64,
    )
    assert changed != CI_BYTES
    with refused("image_identity"):
        fp.declared_image(changed, ff.IMAGE)
    with refused("image_identity"):
        fp.declared_image(b"jobs: {}\n", ff.IMAGE)
    with refused("image_identity"):
        fp.declared_image(b"\xff", ff.IMAGE)


def test_an_unpinned_image_refuses() -> None:
    unpinned = (
        b"jobs:\n  quality-gates:\n    env:\n      %s: postgres:18.6\n"
        % (fp.IMAGE_VARIABLE.encode())
        + b"    services:\n      postgres:\n        image: postgres:18.6\n"
    )
    with refused("image_identity"):
        fp.declared_image(unpinned, "postgres:18.6")


IMAGE = fp.DeclaredImage("postgres:18.6", ff.DIGEST)


def test_the_docker_identity_is_read_from_the_metadata() -> None:
    assert fp.docker_identity(ff.docker_metadata(), IMAGE) == {
        "declared_image": {"reference": "postgres:18.6", "digest": ff.DIGEST},
        "image_id": ff.IMAGE_ID,
        "image_store": {"driver": "overlay2", "containerd_snapshotter": False},
        "platform": {"os": "linux", "architecture": "amd64", "variant": None},
    }
    containerd = ff.docker_metadata(
        info__DriverStatus=[["driver-type", "io.containerd.snapshotter.v1"]]
    )
    assert fp.docker_identity(containerd, IMAGE)["image_store"]["containerd_snapshotter"]
    arm = ff.docker_metadata(image__Architecture="arm64", image__Variant="v8")
    assert fp.docker_identity(arm, IMAGE)["platform"] == {
        "os": "linux",
        "architecture": "arm64",
        "variant": "v8",
    }


def test_an_absent_variant_is_a_typed_null_and_a_named_one_is_kept() -> None:
    metadata = ff.docker_metadata()
    assert "Variant" not in metadata["image"]  # Docker omits an absent variant
    assert fp.docker_identity(metadata, IMAGE)["platform"]["variant"] is None
    named = ff.docker_metadata(image__Architecture="arm64", image__Variant="v8")
    assert fp.docker_identity(named, IMAGE)["platform"]["variant"] == "v8"


@pytest.mark.parametrize("variant", ["", None, 0, 8, True, False, ["v8"], {"v": 8}])
def test_a_present_variant_that_is_not_a_nonempty_string_refuses(variant: object) -> None:
    metadata = ff.docker_metadata()
    metadata["image"]["Variant"] = variant
    with refused("image_identity"):
        fp.docker_identity(metadata, IMAGE)


@pytest.mark.parametrize(
    "changes",
    [
        {"container__Image": "sha256:" + "cd" * 32},
        {"image__RepoDigests": ["postgres@sha256:" + "0" * 64]},
        {"image__RepoDigests": []},
        {"image__Id": None},
        {"image__Os": None},
        {"image__Os": ""},
        {"image__Architecture": None},
        {"info__Driver": None},
        {"info__DriverStatus": "overlay2"},
        {"image__Variant": 7},
    ],
)
def test_any_image_identity_failure_refuses(changes: dict[str, Any]) -> None:
    with refused("image_identity"):
        fp.docker_identity(ff.docker_metadata(**changes), IMAGE)


def test_the_architecture_must_agree_with_the_build_target_of_version() -> None:
    docker = fp.docker_identity(ff.docker_metadata(), IMAGE)
    engine = fp.engine_group(docker, 180006, ff.VERSION, {"plpgsql": "1.0"})
    assert engine["server_version_num"] == 180006
    with refused("image_identity"):
        fp.engine_group(docker, 180006, ff.VERSION.replace("x86_64", "aarch64"), {})
    with refused("image_identity"):
        fp.engine_group(docker, 180006, "PostgreSQL 18.6 on sparc64-sun-solaris", {})
    with refused("image_identity"):
        fp.engine_group(docker, 180006, "PostgreSQL 18.6", {})


# --- the installed set (§12.3 python) -------------------------------------------------------------

DEV = ROOT / "backend" / "requirements-dev-lock.txt"
RUNTIME = ROOT / "backend" / "requirements-lock.txt"
LINUX = {
    "sys_platform": "linux",
    "platform_system": "Linux",
    "platform_machine": "x86_64",
    "os_name": "posix",
    "implementation_name": "cpython",
    "platform_python_implementation": "CPython",
    "python_version": "3.14",
    "python_full_version": "3.14.7",
}


def test_the_real_locks_parse_into_exact_pins_and_agree() -> None:
    dev = fp.parse_lock(DEV.read_text(encoding="utf-8"), "dev")
    runtime = fp.parse_lock(RUNTIME.read_text(encoding="utf-8"), "runtime")
    assert len(dev) > len(runtime) > 30
    dev_pins = {(pin.name, pin.version) for pin in dev}
    assert all((pin.name, pin.version) in dev_pins for pin in runtime)
    linux = fp.applicable(dev, "dev", LINUX)
    assert "packaging" in linux and "pyyaml" in linux
    assert "colorama" not in linux and "uvloop" in linux


def test_the_ci_install_of_the_dev_lock_is_exactly_the_installed_set() -> None:
    dev, runtime = DEV.read_text(encoding="utf-8"), RUNTIME.read_text(encoding="utf-8")
    installed = fp.applicable(fp.parse_lock(dev, "dev"), "dev", LINUX)
    packages = fp.installed_set(dev, runtime, {**installed, "pip": "26.0"}, LINUX)
    assert [item["name"] for item in packages] == sorted(installed)
    assert "pip" not in {item["name"] for item in packages}


LOCK = (
    "alpha==1.0 \\\n    --hash=sha256:aa\n"
    "beta==2.0 ; sys_platform == 'win32' \\\n    --hash=sha256:bb\n"
)


def test_a_marker_that_does_not_hold_excludes_its_pin() -> None:
    assert fp.installed_set(LOCK, "alpha==1.0\n", {"alpha": "1.0"}, LINUX) == [
        {"name": "alpha", "version": "1.0"}
    ]


@pytest.mark.parametrize(
    "installed",
    [
        {},
        {"alpha": "1.1"},
        {"alpha": "1.0", "gamma": "3.0"},
        {"alpha": "1.0", "beta": "2.0"},
    ],
)
def test_an_installed_set_that_is_not_the_lock_refuses(installed: dict[str, str]) -> None:
    with refused("installed_set"):
        fp.installed_set(LOCK, "alpha==1.0\n", installed, LINUX)


def test_a_runtime_pin_that_differs_from_the_dev_lock_refuses() -> None:
    with refused("installed_set"):
        fp.installed_set(LOCK, "alpha==1.1\n", {"alpha": "1.0"}, LINUX)
    with refused("installed_set"):
        fp.installed_set(LOCK, "delta==1.0\n", {"alpha": "1.0"}, LINUX)


@pytest.mark.parametrize(
    "lock",
    [
        "alpha>=1.0\n",
        "alpha==1.*\n",
        "alpha[extra]==1.0\n",
        "alpha @ https://example.test/alpha.whl\n",
        "--index-url https://example.test\n",
        "alpha==1.0 ; nonsense ==\n",
        "\n# nothing\n",
    ],
)
def test_anything_but_an_exact_pin_refuses(lock: str) -> None:
    with refused("installed_set"):
        fp.parse_lock(lock, "lock")


def test_two_applicable_pins_of_one_name_refuse() -> None:
    lock = "alpha==1.0 ; sys_platform == 'linux'\nalpha==2.0 ; os_name == 'posix'\n"
    with refused("installed_set"):
        fp.applicable(fp.parse_lock(lock, "lock"), "lock", LINUX)


def make_distribution(site: Path, *, tamper: bool = False, drop: bool = False) -> Any:
    package = site / "fakepkg"
    package.mkdir(parents=True)
    content = b"VALUE = 1\n"
    (package / "__init__.py").write_bytes(content)
    info = site / "fakepkg-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: FakePkg\nVersion: 1.0\n")
    import base64

    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
    (info / "RECORD").write_text(
        f"fakepkg/__init__.py,sha256={digest},{len(content)}\n"
        "fakepkg-1.0.dist-info/METADATA,,\nfakepkg-1.0.dist-info/RECORD,,\n"
    )
    if tamper:
        (package / "__init__.py").write_bytes(b"VALUE = 2\n")
    if drop:
        (package / "__init__.py").unlink()
    return importlib.metadata.PathDistribution(info)


def test_record_verification_passes_an_untouched_distribution(tmp_path: Path) -> None:
    distribution = make_distribution(tmp_path)
    assert fp.record_problems(distribution) == []
    assert fp.installed_distributions([distribution]) == {"fakepkg": "1.0"}


def test_record_verification_catches_a_changed_or_missing_file(tmp_path: Path) -> None:
    changed = make_distribution(tmp_path / "a", tamper=True)
    assert fp.record_problems(changed) == [
        "FakePkg: fakepkg/__init__.py does not match its RECORD hash"
    ]
    with refused("installed_set"):
        fp.installed_distributions([changed])
    missing = make_distribution(tmp_path / "b", drop=True)
    assert fp.record_problems(missing) == ["FakePkg: fakepkg/__init__.py is missing"]


def test_a_distribution_without_record_or_installed_twice_refuses(tmp_path: Path) -> None:
    distribution = make_distribution(tmp_path / "a")
    (Path(str(distribution.locate_file("fakepkg-1.0.dist-info"))) / "RECORD").unlink()
    assert fp.record_problems(distribution) == ["FakePkg: no RECORD"]
    first, second = make_distribution(tmp_path / "b"), make_distribution(tmp_path / "c")
    with refused("installed_set"):
        fp.installed_distributions([first, second])


def test_the_python_group_records_the_interpreter() -> None:
    group = fp.python_group("alpha==1.0\n", "alpha==1.0\n", {"alpha": "1.0", "pip": "26.0"})
    canon.check(group, fp.PYTHON_SPEC)
    assert group["installed_set_sha256"] == canon.sha256([{"name": "alpha", "version": "1.0"}])
    assert group["implementation"] == "cpython"
    assert group["float_format"] == {
        "mant_dig": 53,
        "radix": 2,
        "max_exp": 1024,
        "repr_style": "short",
    }
    assert group["version"] == sys.version.split()[0]
    assert group["build"] == sys.version
    import decimal

    assert group["decimal"] == {
        "module": decimal.__version__,
        "libmpdec": decimal.__libmpdec_version__,
    }


def test_the_python_group_refuses_without_c_decimal(monkeypatch: pytest.MonkeyPatch) -> None:
    def python_decimal() -> None:
        raise rn.NumericVerificationError("_pydecimal")

    monkeypatch.setattr(fp, "require_c_decimal", python_decimal)
    with refused("numeric_certificate"):
        fp.python_group(LOCK, "alpha==1.0\n", {"alpha": "1.0"})


# --- the sign-off precondition (§12.1, §12.7) -----------------------------------------------------


def signoff_texts() -> dict[str, str]:
    return {
        "record_text": so.SIGNOFF_PATH.read_bytes().decode("utf-8"),
        "sheet_text": rr.SHEET_PATH.read_bytes().decode("utf-8"),
        "form_text": rr.FORM_PATH.read_bytes().decode("utf-8"),
        "decision_record_text": so.DECISION_RECORD_PATH.read_bytes().decode("utf-8"),
    }


def test_the_signed_dataset_passes_the_precondition(dataset: RetrievalDataset) -> None:
    texts = signoff_texts()
    precondition = fp.signoff_precondition(dataset, **texts)
    assert precondition == {
        "file": "tests/evaluation/datasets/knowledge_retrieval_v2.signoff.json",
        "sha256": hashlib.sha256(
            texts["record_text"].replace("\r\n", "\n").encode("utf-8")
        ).hexdigest(),
        "decision": "approved",
        "dataset_sha256": DATASET_SHA256,
    }


@pytest.mark.parametrize(
    "tamper",
    [
        lambda t: {**t, "record_text": "{"},
        lambda t: {**t, "record_text": ""},
        lambda t: {**t, "record_text": t["record_text"].replace('"approved"', '"rejected"')},
        lambda t: {**t, "record_text": t["record_text"].replace(DATASET_SHA256, "0" * 64)},
        lambda t: {**t, "sheet_text": t["sheet_text"] + "\nedited\n"},
        lambda t: {**t, "form_text": t["form_text"].replace("accept", "revise", 1)},
        lambda t: {
            **t,
            "decision_record_text": t["decision_record_text"].replace(
                "### 3.5 Relevance grades", "### 3.5 Relevance grades\n\nEdited.", 1
            ),
        },
    ],
)
def test_a_missing_or_mismatched_sign_off_refuses(dataset: RetrievalDataset, tamper: Any) -> None:
    with refused("signoff"):
        fp.signoff_precondition(dataset, **tamper(signoff_texts()))


# --- the environment record (§12.1) ---------------------------------------------------------------


def test_the_environment_record_lists_what_it_cannot_read_and_is_canonical() -> None:
    import datetime

    now = datetime.datetime(2026, 11, 12, 12, 0, tzinfo=datetime.UTC)
    record = fp.environment_record(
        None,
        started=now,
        finished=now,
        database={"settings": [["search_path", "public", "default"]]},
        environ={"PYTHONHASHSEED": "0", "GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "2"},
    )
    assert None not in record.values()
    assert {"commit", "tree", "github_sha", "runner_image", "platform_manifest_digest"} <= set(
        record["unavailable"]
    )
    tree = "a" * 40
    assert fp.environment_record("b" * 40, tree=tree, started=now, finished=now)["tree"] == tree
    assert record["pythonhashseed"] == "0"
    assert record["ci_run_id"] == "1" and record["ci_run_attempt"] == "2"
    assert record["postgres_settings"] == [["search_path", "public", "default"]]
    assert record["started"] == "2026-11-12T12:00:00+00:00"
    canon.loads(canon.dumps(record))


# --- determinism across hash seeds (§12.5 hash-seed rule applied to the fingerprint) --------------

_PROBE = """
import hashlib, sys
from tests.evaluation import retrieval_canonical as canon, retrieval_fingerprint as fp
from tests.evaluation import retrieval_v2 as v2
from tests.evaluation import fingerprint_fixtures as ff, retrieval_probes as rp
dataset = v2.load()
db = ff.database(dataset)
image = fp.DeclaredImage("postgres:18.6", ff.DIGEST)
value = {"metrics": fp.metrics_group(), "dataset": fp.dataset_group(dataset),
         "engine": fp.docker_identity(ff.docker_metadata(), image),
         "database": rp.database_group(db),
         "text_search": rp.text_search_group(db, ["greek", "english"]),
         "sets": canon.sorted_unique([{"b": 1}, {"a": 2}, "z", "y"])}
sys.stdout.buffer.write(canon.dumps(value))
sys.stderr.write(str(hash("retrieval_fingerprint_v1")))
"""


def test_the_fingerprint_inputs_do_not_depend_on_the_hash_seed() -> None:
    outputs, hashes = [], []
    for seed in ("0", "1", "4294967295", "random"):
        done = subprocess.run(
            [sys.executable, "-c", _PROBE],
            capture_output=True,
            check=True,
            cwd=ROOT,
            env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": f"backend{os.pathsep}."},
        )
        outputs.append(done.stdout)
        hashes.append(done.stderr)
    assert len(set(outputs)) == 1
    assert len(set(hashes[:3])) == 3
    assert json.loads(outputs[0])["dataset"]["sha256"] == DATASET_SHA256
    # Three of §12.2's four nulls are typed nulls in the bytes every seed agrees on.
    assert b'"locale":null' in outputs[0] and b'"init_options":null' in outputs[0]
    assert b'"variant":null' in outputs[0]
