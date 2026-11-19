"""R6.2: every count the README states is computed from the code, so none can go stale.

The README's figures were maintained by hand, and they drifted: it said "7984 backend tests" long
after the suite had grown past 12,000, and "15 model modules" when there were 17. Each figure it
states about the code is checked here against the code itself -- the OpenAPI document, the ORM
metadata, the migration chain, the production build's verifier -- in the normal suite, so CI
fails the commit that makes one wrong. The mutation total is checked by
`tests/mutation/test_inventory_docs.py`.

Two kinds of figure are not checked here, on purpose. The screenshot caption's demo-dataset sizes
describe the data the committed images were captured on, not anything the code computes; and
counts written as words ("Eleven domains") are prose.

Test-suite sizes are deliberately not stated: CI runs the backend suite in three shards and the
frontend suite in another job, so no single job can compute the total, and a figure no test can
check is the kind that went stale. The README must not reintroduce one.

Matching ignores line wrapping and blockquote markers, because prose is re-wrapped freely.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest

import app.models  # noqa: F401  -- registers every mapped class
from app.core.config import Settings
from app.db.base import Base
from app.main import create_app

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
README = REPOSITORY_ROOT / "README.md"
VERSIONS = REPOSITORY_ROOT / "database" / "migrations" / "versions"
PRODUCTION_BUILDER = REPOSITORY_ROOT / "ml" / "pipelines" / "build_production_artifact.py"
METHODS = {"get", "post", "put", "patch", "delete"}


def flat(text: str) -> str:
    """One line, with blockquote markers dropped, so a re-wrapped sentence still matches."""
    return re.sub(r"\s+", " ", re.sub(r"\n[ \t]*>", "\n", text))


@pytest.fixture(scope="module")
def readme() -> str:
    return flat(README.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def openapi() -> dict[str, Any]:
    app = create_app(Settings(environment="test", secret_key="readme-counts-test-secret"))
    return app.openapi()


def operations(openapi: dict[str, Any]) -> list[dict[str, Any]]:
    return [op for item in openapi["paths"].values() for m, op in item.items() if m in METHODS]


def migration_chain() -> list[str]:
    """The revisions in order from the first, requiring one linear chain."""
    down: dict[str, str | None] = {}
    for path in sorted(VERSIONS.glob("*.py")):
        values: dict[str, Any] = {}
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Assign):
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                target, value = node.target, node.value
            else:
                continue
            if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                values[target.id] = ast.literal_eval(value)
        down[values["revision"]] = values["down_revision"]
    heads = set(down) - {d for d in down.values() if d is not None}
    assert len(heads) == 1, f"not one head: {sorted(heads)}"
    chain = [heads.pop()]
    while down[chain[-1]] is not None:
        parent = down[chain[-1]]
        assert isinstance(parent, str)
        chain.append(parent)
    assert len(chain) == len(down), "a revision outside the chain"
    return chain[::-1]


def stated(readme: str, pattern: str) -> list[tuple[str, ...]]:
    found = re.findall(pattern, readme)
    assert found, f"the README no longer states {pattern!r}; update this test with it"
    return [f if isinstance(f, tuple) else (f,) for f in found]


def test_api_paths_and_operations(readme: str, openapi: dict[str, Any]) -> None:
    computed = (str(len(openapi["paths"])), str(len(operations(openapi))))
    for found in stated(readme, r"(\d+) API paths / (\d+) operations"):
        assert found == computed
    for found in stated(readme, r"(\d+) paths, (\d+) operations"):
        assert found == computed


def test_operations_that_require_authentication(readme: str, openapi: dict[str, Any]) -> None:
    secured = sum(1 for op in operations(openapi) if op.get("security"))
    for (found,) in stated(readme, r"(\d+) require authentication"):
        assert int(found) == secured


def test_application_tables(readme: str) -> None:
    tables = len(Base.metadata.tables)
    for (found,) in stated(readme, r"(\d+)-table PostgreSQL"):
        assert int(found) == tables
    for (found,) in stated(readme, r"(\d+) application tables"):
        assert int(found) == tables


def test_model_modules(readme: str) -> None:
    modules = {mapper.class_.__module__ for mapper in Base.registry.mappers}
    for (found,) in stated(readme, r"(\d+) model modules"):
        assert int(found) == len(modules)


def approved_value_checks() -> int:
    """How many approved values the production build compares, read from `verify` itself.

    `verify` makes one `checks.equal` call per value, with no loop or branch around them, so the
    calls written are the comparisons run -- the build prints the same number on success. The
    no-control-flow condition is asserted, so a refactor that loops cannot make this miscount.
    """
    tree = ast.parse(PRODUCTION_BUILDER.read_text(encoding="utf-8"))
    (verify,) = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "verify"]
    branches = [n for n in ast.walk(verify) if isinstance(n, (ast.For, ast.While, ast.If, ast.Try))]
    assert branches == [], "verify() branches or loops; count its comparisons another way"
    return sum(
        1
        for n in ast.walk(verify)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "equal"
    )


def test_approved_values_the_production_build_verifies(readme: str) -> None:
    checks = approved_value_checks()
    for (found,) in stated(readme, r"(\d+) approved values"):
        assert int(found) == checks


def test_migrations_and_head(readme: str) -> None:
    chain = migration_chain()
    for head, count in stated(
        readme, r"Schema head is `([0-9a-z_]+)` across (\d+) linear migrations"
    ):
        assert (head, int(count)) == (chain[-1], len(chain))
    for count, head in stated(readme, r"(\d+) linear revisions, head `([0-9a-z_]+)`"):
        assert (head, int(count)) == (chain[-1], len(chain))


def test_no_test_suite_size_is_stated(readme: str) -> None:
    """No job can check such a figure, so the README must not state one."""
    assert re.findall(r"\d[\d,]* (?:backend|frontend) tests", readme) == []


def test_the_matchers_see_the_old_stale_sentence() -> None:
    """A positive control: the absence check would have caught the sentence it replaced."""
    stale = flat("**7984 backend tests and 1346 frontend tests pass in CI.**")
    assert re.findall(r"\d[\d,]* (?:backend|frontend) tests", stale) == [
        "7984 backend tests",
        "1346 frontend tests",
    ]
