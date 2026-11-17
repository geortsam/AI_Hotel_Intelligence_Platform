"""R2.10C -- decision F §12.6 "C.4's requirements" and the exclusion audit's static checks.

The exclusion audit accepted the 38 modules §12.3 excludes on three conditions the C.4
integration test must meet, and on four properties of the code that these tests hold:

- **A.** Requests reach the database only through the integration conftest's `get_db` override,
  bound to the engine `fetch` reads; the production session factory refuses while measuring.
- **B.** `UploadRecord.hotels` comes from SQL over the hotels' public ids, never the ORM.
- **C.** Every ingest and search response passes `checked_json` with the status it must have.

Static checks use the `ast` module, not text matching. What can only be checked once C.4 writes
its test (that the test calls these helpers) is §12.6's C.4 acceptance list, not a placeholder.
Offline: an in-memory SQLite engine stands in where an engine object is needed, never connected
to PostgreSQL.
"""

from __future__ import annotations

import ast
import dataclasses
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import configure_mappers

from tests.evaluation import fingerprint_fixtures as ff
from tests.evaluation import retrieval_artifacts as ra
from tests.evaluation import retrieval_fingerprint as fp
from tests.evaluation import retrieval_probes as rp
from tests.evaluation import retrieval_v2 as v2
from tests.evaluation.retrieval_fingerprint import MeasurementRefused
from tests.evaluation.retrieval_schema import RetrievalDataset

ROOT = fp.REPOSITORY_ROOT
APP = ROOT / "backend" / "app"


def refused(reason: str, match: str = "") -> Any:
    return pytest.raises(MeasurementRefused, match=f"^{reason}: .*{match}")


@pytest.fixture(scope="module")
def dataset() -> RetrievalDataset:
    return v2.load()


@pytest.fixture(scope="module")
def repository(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return ff.make_repository(tmp_path_factory.mktemp("c4") / "repo")


@pytest.fixture
def engine() -> Iterator[sa.Engine]:
    engine = sa.create_engine("sqlite://")
    yield engine
    engine.dispose()


def conftest_app(engine: sa.Engine) -> Any:
    from tests.integration.conftest import create_test_app

    return create_test_app(engine)


# --- requirement A: one engine, the conftest's, and no fallback ---------------------------------


def test_engine_fetch_reads_rows_through_its_engine(engine: sa.Engine) -> None:
    fetch = ra.EngineFetch(engine)
    assert fetch.engine is engine
    assert fetch("SELECT :a AS a, 2 AS b", {"a": 1}) == [(1, 2)]


def test_the_conftest_app_reads_through_the_conftest_engine(engine: sa.Engine) -> None:
    ra.require_measurement_wiring(conftest_app(engine), ra.EngineFetch(engine))


def test_a_missing_get_db_override_refuses(engine: sa.Engine) -> None:
    app = conftest_app(engine)
    app.dependency_overrides.clear()
    with refused("unmeasurable", "get_db is not overridden"):
        ra.require_measurement_wiring(app, ra.EngineFetch(engine))


def test_an_override_bound_to_another_engine_refuses(engine: sa.Engine) -> None:
    other = sa.create_engine("sqlite://")
    try:
        with refused("unmeasurable", "different engines"):
            ra.require_measurement_wiring(conftest_app(engine), ra.EngineFetch(other))
    finally:
        other.dispose()


def test_a_fetch_without_an_engine_refuses(engine: sa.Engine, dataset: RetrievalDataset) -> None:
    with refused("unmeasurable", "fetch is not bound to an engine"):
        ra.require_measurement_wiring(conftest_app(engine), ff.database(dataset))


def test_the_production_session_factory_refuses_while_measuring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import deps
    from app.db import session as db_session

    def no_production_engine() -> Any:
        raise AssertionError("the production engine must never be built here")

    monkeypatch.setattr(db_session, "get_engine", no_production_engine)
    original = deps.get_session_factory
    with ra.production_sessions_forbidden():
        sessions = deps.get_db()
        with refused("unmeasurable", "production session factory"):
            next(sessions)
    assert deps.get_session_factory is original


# --- requirement B: hotel ownership by SQL -------------------------------------------------------


def test_hotel_ids_are_read_by_sql_from_public_ids(dataset: RetrievalDataset) -> None:
    db = ff.database(dataset)
    assert rp.hotel_ids(db, ff.HOTEL_PUBLIC_IDS) == {1: "evaluated", 2: "neighbour"}
    assert db.calls.count(rp.HOTEL_ID_SQL) == 2


@pytest.mark.parametrize(
    "case", ["missing role", "extra role", "one row for both", "no row", "not an integer"]
)
def test_hotel_ids_refuse_anything_but_two_distinct_rows(
    dataset: RetrievalDataset, case: str
) -> None:
    db = ff.database(dataset)
    public_ids = dict(ff.HOTEL_PUBLIC_IDS)
    if case == "missing role":
        del public_ids["neighbour"]
    elif case == "extra role":
        public_ids["third"] = "6b1f4c1e-0000-4000-8000-000000000003"
    elif case == "one row for both":
        db.overrides[rp.HOTEL_ID_SQL] = lambda _p: [(1,)]
    elif case == "no row":
        db.overrides[rp.HOTEL_ID_SQL] = lambda _p: []
    else:
        db.overrides[rp.HOTEL_ID_SQL] = lambda p: [
            (str(ff.HOTEL_PUBLIC_IDS_TO_ROWS[p["public_id"]]),)
        ]
    with pytest.raises(MeasurementRefused):
        rp.hotel_ids(db, public_ids)


def measure_with(
    dataset: RetrievalDataset,
    repository: Path,
    out: Path,
    db: ff.FakeDatabase,
    *,
    search: Any = None,
    ingest: Any = None,
) -> dict[str, Any]:
    ff.write_docker(out)
    base = ff.harness(dataset, db)
    harness = ra.Harness(fetch=db, ingest=ingest or base.ingest, search=search or base.search)
    return ra.run_measurement(
        harness,
        out,
        repository=repository,
        environ=ff.environ(),
        installed=ff.installed_from_lock(),
        require_imports=False,
    )


def sql_ingest(db: ff.FakeDatabase) -> Any:
    """C.4's ingest as requirement B requires: the hotels by SQL from their public ids."""

    def ingest(_dataset: RetrievalDataset) -> rp.UploadRecord:
        hotels = rp.hotel_ids(db, ff.HOTEL_PUBLIC_IDS)
        return dataclasses.replace(db.ingested.uploads, hotels=hotels)

    return ingest


def test_the_sql_mapping_decides_ownership_in_the_state_check(
    dataset: RetrievalDataset, repository: Path, tmp_path: Path
) -> None:
    db = ff.database(dataset)
    report = measure_with(dataset, repository, tmp_path / "right", db, ingest=sql_ingest(db))
    assert report["fingerprint"] == canon_fingerprint(report)
    # The SQL answer swapped: every document now sits under the other role, and the run refuses.
    swapped = ff.database(dataset)
    rows = {ff.HOTEL_PUBLIC_IDS["evaluated"]: 2, ff.HOTEL_PUBLIC_IDS["neighbour"]: 1}
    swapped.overrides[rp.HOTEL_ID_SQL] = lambda p: [(rows[p["public_id"]],)]
    out = tmp_path / "swapped"
    with pytest.raises(MeasurementRefused) as caught:
        measure_with(dataset, repository, out, swapped, ingest=sql_ingest(swapped))
    assert caught.value.reason == "mismatch"
    assert (out / ra.REFUSAL_FILE).is_file() and not (out / ra.REPORT_FILE).exists()


def canon_fingerprint(report: dict[str, Any]) -> str:
    return fp.fingerprint(report["inputs"])


def test_no_harness_module_reads_hotels_through_the_orm() -> None:
    for path in fp.HARNESS_SOURCES:
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        imported = [
            node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        ] + [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ]
        assert not [name for name in imported if name.startswith("app.models")], path


# --- requirement C: an explicit status for every response ---------------------------------------


@dataclasses.dataclass
class Response:
    status_code: Any
    body: Any = None
    broken: bool = False

    def json(self) -> Any:
        if self.broken:
            raise ValueError("not JSON")
        return self.body


def test_checked_json_returns_the_object_for_the_expected_status() -> None:
    assert ra.checked_json(Response(200, {"query": "q"}), 200, "search") == {"query": "q"}
    assert ra.checked_json(Response(201, {"id": 1}), 201, "upload") == {"id": 1}


@pytest.mark.parametrize(
    ("status", "expected"),
    [(201, 200), (200, 201), (404, 200), (422, 201), (429, 200), (500, 200), (503, 201), (True, 1)],
)
def test_an_unexpected_status_refuses(status: Any, expected: int) -> None:
    with refused("unmeasurable", f"HTTP {status}, expected {expected}"):
        ra.checked_json(Response(status, {"results": []}), expected, "search")


def test_a_body_that_is_not_a_json_object_refuses() -> None:
    with refused("unmeasurable", "not JSON"):
        ra.checked_json(Response(200, broken=True), 200, "search")
    bodies: tuple[Any, ...] = ([], "results", None, 3)
    for body in bodies:
        with refused("unmeasurable", "not a JSON object"):
            ra.checked_json(Response(200, body), 200, "search")


ERROR_ENVELOPE = {"error": {"code": "INTERNAL_ERROR", "message": "x", "details": []}}


def test_a_failed_search_response_stops_the_measurement(
    dataset: RetrievalDataset, repository: Path, tmp_path: Path
) -> None:
    def search(_text: str, _limit: int) -> dict[str, Any]:
        return ra.checked_json(Response(500, ERROR_ENVELOPE), 200, "search")

    out = tmp_path / "out"
    with refused("unmeasurable", "search: HTTP 500"):
        measure_with(dataset, repository, out, ff.database(dataset), search=search)
    assert {p.name for p in out.iterdir()} == ra.REFUSAL_SET


def test_a_failed_ingest_response_stops_the_measurement(
    dataset: RetrievalDataset, repository: Path, tmp_path: Path
) -> None:
    def ingest(_dataset: RetrievalDataset) -> Any:
        return ra.checked_json(Response(429, ERROR_ENVELOPE), 201, "upload")

    out = tmp_path / "out"
    with refused("unmeasurable", "upload: HTTP 429"):
        measure_with(dataset, repository, out, ff.database(dataset), ingest=ingest)
    assert {p.name for p in out.iterdir()} == ra.REFUSAL_SET


def test_an_error_envelope_never_passes_as_a_search_result(
    dataset: RetrievalDataset, repository: Path, tmp_path: Path
) -> None:
    """Even a 200 carrying the error envelope refuses: it echoes no query and no limit."""
    out = tmp_path / "out"
    with refused("served", "the echo"):
        measure_with(
            dataset, repository, out, ff.database(dataset), search=lambda _t, _l: ERROR_ENVELOPE
        )


# --- the exclusion audit's static checks ---------------------------------------------------------


def references(tree: ast.AST, names: frozenset[str]) -> bool:
    """Whether *tree* names any of *names*: as a name, an attribute or an imported alias."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in names:
            return True
        if isinstance(node, ast.Attribute) and node.attr in names:
            return True
        if isinstance(node, ast.ImportFrom) and any(a.name in names for a in node.names):
            return True
    return False


def app_files() -> dict[str, ast.AST]:
    return {
        path.relative_to(APP).as_posix(): ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(APP.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def test_the_production_engine_is_reached_only_through_get_db_and_the_jobs() -> None:
    files = app_files()
    process_wide = frozenset({"get_session_factory", "get_engine"})
    assert sorted(path for path, tree in files.items() if references(tree, process_wide)) == [
        "api/deps.py",
        "db/session.py",
    ]
    # In deps.py, only `get_db` calls the factory (besides the import itself).
    deps = files["api/deps.py"]
    callers = sorted(
        function.name
        for function in ast.walk(deps)
        if isinstance(function, ast.FunctionDef) and references(function, process_wide)
    )
    assert callers == ["get_db"]
    builders = frozenset({"create_db_engine", "create_session_factory"})
    allowed = {"db/session.py", "db/__init__.py"}
    for path, tree in files.items():
        if references(tree, builders):
            assert path in allowed or path.startswith("jobs/"), path


#: The 38 exclusions the audit accepted: every exclusion but the three §12.3 named before it.
AUDITED = tuple(
    path
    for path in fp.EXCLUDED_SOURCES
    if path
    not in {
        "database/migrations/script.py.mako",
        "tests/db_safety.py",
        "backend/app/core/config.py",
    }
)


def forbidden_uses(tree: ast.AST) -> list[str]:
    """Event listeners, environment reads and dynamic imports, by syntax."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in {"listen", "listens_for"}:
            found.append(f"event.{node.attr}")
        elif isinstance(node, ast.Name) and node.id in {"listens_for", "__import__", "getenv"}:
            found.append(node.id)
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
            and node.attr in {"environ", "getenv", "putenv", "environb"}
        ):
            found.append(f"os.{node.attr}")
        elif isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.split(".")[0] == "importlib"]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = {a.name for a in node.names}
            if module.split(".")[0] == "importlib" or module == "sqlalchemy.event":
                found.append(f"from {module}")
            if module == "sqlalchemy" and "event" in names:
                found.append("from sqlalchemy import event")
            if module == "os" and names & {"environ", "getenv", "putenv", "environb"}:
                found.append("from os import environ")
    return found


def test_the_audited_exclusions_register_no_listeners_read_no_environment_import_nothing() -> None:
    assert len(AUDITED) == 38
    for path in AUDITED:
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        assert forbidden_uses(tree) == [], path


def test_the_static_check_sees_each_forbidden_use() -> None:
    for source in (
        "from sqlalchemy import event\n",
        "import sqlalchemy\nsqlalchemy.event.listen(object, 'x', print)\n",
        "@listens_for(object, 'x')\ndef f(): ...\n",
        "import os\nx = os.environ['A']\n",
        "import os\nx = os.getenv('A')\n",
        "from os import environ\n",
        "import importlib\n",
        "from importlib import import_module\n",
        "m = __import__('json')\n",
    ):
        assert forbidden_uses(ast.parse(source)), source


def test_the_knowledge_models_have_no_relationships_and_none_point_at_them() -> None:
    import app.models  # noqa: F401 - every model, as the application registers them
    from app.db.base import Base
    from app.models.knowledge import HotelDocument, HotelDocumentChunk

    configure_mappers()
    knowledge = {HotelDocument, HotelDocumentChunk}
    for model in knowledge:
        assert list(sa.inspect(model).relationships) == [], model
    for mapper in Base.registry.mappers:
        for relationship in mapper.relationships:
            assert relationship.mapper.class_ not in knowledge, (mapper.class_, relationship.key)
