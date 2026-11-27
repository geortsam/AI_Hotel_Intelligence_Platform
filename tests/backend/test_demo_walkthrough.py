"""The scripted demo walkthrough, held to the repository it describes.

`docs/demo-walkthrough.md` (2026-11-27) takes a reviewer from a fresh clone through every screen
and into the code. A walkthrough rots quietly: a renamed script, a moved file, a removed route or a
changed default leaves it confidently wrong. So this file holds it to the code, not to itself:

* its structure: the five parts the brief asked for, and the four answers for every core step;
* every repository path, `python -m` module, screen route and API route it names exists;
* the demo account is the seed's own default, and no password is written down anywhere in it;
* the Docker seeding command is the one CI's responsive-audit job runs on every push;
* the synthetic demo data is kept apart from the published dataset the model was evaluated on;
* the portfolio scope and the limitations are stated, and production readiness never claimed;
* the Analytics expenses-table clipping is described as fixed, not open;
* the README and the documentation index both lead to it.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.main import create_app
from scripts import seed_demo
from tests.backend.test_doc_links import index_entries
from tests.backend.test_readme_counts import METHODS, migration_chain

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WALKTHROUGH = REPOSITORY_ROOT / "docs" / "demo-walkthrough.md"
README = REPOSITORY_ROOT / "README.md"
INDEX = REPOSITORY_ROOT / "docs" / "README.md"
CI = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
ROUTES = REPOSITORY_ROOT / "frontend" / "src" / "router" / "routes.ts"
TRAINING_DATA = REPOSITORY_ROOT / "docs" / "ml-training-data.md"

#: The parts the walkthrough must have, in order.
PARTS = (
    "## How this walkthrough was verified",
    "## Two datasets, never mixed",
    "## A. Preparation",
    "## B. Authentication",
    "## C. Core application walkthrough",
    "## D. Technical demonstration",
    "## E. Reset and cleanup",
    "## Limitations to state during the demo",
)

#: The fourteen core steps, each a screen or a capability that exists.
CORE_STEPS = 14

#: Paths it names that are not in the repository, on purpose, each with the reason.
NOT_COMMITTED = {
    "ml/models/demand_baseline_v1/model.pkl": "the generated payload, never committed",
    "certs/": "the local certificate directory the reader creates",
}

#: The top-level areas a repository path starts with.
ROOTS = ("backend/", "frontend/", "scripts/", "tests/", "ml/", "database/", "docs/", ".github/")


def flat(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\n[ \t]*>", "\n", text))


@pytest.fixture(scope="module")
def text() -> str:
    return WALKTHROUGH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def openapi() -> dict[str, Any]:
    app = create_app(Settings(environment="test", secret_key="walkthrough-test-secret"))
    return app.openapi()


def section(text: str, heading: str) -> str:
    start = text.index(heading)
    following = text.find("\n## ", start + 1)
    return text[start : following if following != -1 else len(text)]


def core_steps(text: str) -> dict[str, str]:
    """`### C<n>.` sections of part C, by number."""
    part = section(text, "## C. Core application walkthrough")
    found = re.split(r"\n(?=### C\d+\. )", part)
    return {re.match(r"### (C\d+)\.", s).group(1): s for s in found if re.match(r"### C\d+\.", s)}  # type: ignore[union-attr]


def code_spans(text: str) -> list[str]:
    return re.findall(r"`([^`\n]+)`", text)


# --- structure ------------------------------------------------------------------------------


def test_the_walkthrough_has_every_required_part_in_order(text: str) -> None:
    positions = [text.index(part) for part in PARTS]
    assert positions == sorted(positions)
    preparation = section(text, "## A. Preparation")
    for heading in ("### A.1 Prerequisites", "### Route A: the local stack", "### Route B: Docker"):
        assert heading in preparation


def test_every_core_step_says_where_what_to_expect_and_what_it_demonstrates(text: str) -> None:
    steps = core_steps(text)
    assert sorted(steps, key=lambda s: int(s[1:])) == [f"C{n}" for n in range(1, CORE_STEPS + 1)]
    for name, body in steps.items():
        assert "- **Navigate:**" in body, name
        assert "- **Expect" in body, name
        assert "- **Demonstrates:**" in body, name


# --- what it names exists -------------------------------------------------------------------


def named_paths(text: str) -> set[str]:
    paths: set[str] = set()
    for span in code_spans(text):
        for token in span.split():
            token = token.strip("\"'(),")
            if token.startswith(ROOTS) or token in NOT_COMMITTED:
                paths.add(token)
    for target in re.findall(r"\]\((\.\./[^)#\s]+|[\w./-]+\.md)(?:#[^)]*)?\)", text):
        resolved = (WALKTHROUGH.parent / target).resolve()
        paths.add(resolved.relative_to(REPOSITORY_ROOT).as_posix())
    return paths


def test_every_repository_path_it_names_exists(text: str) -> None:
    paths = named_paths(text)
    # Guards the assertion below: an extraction that found nothing would pass silently.
    assert {"scripts/seed_demo.py", "tests/evaluation/retrieval_report_v2.json"} <= paths
    for path in sorted(paths):
        if path in NOT_COMMITTED:
            continue
        if path.endswith("*"):
            assert list(REPOSITORY_ROOT.glob(path)), path
        else:
            assert (REPOSITORY_ROOT / path).exists(), path


def test_the_paths_it_says_are_not_committed_are_git_ignored() -> None:
    ignored = (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "certs/" in ignored
    assert "ml/models/*/*" in ignored
    assert set(NOT_COMMITTED) == {"certs/", "ml/models/demand_baseline_v1/model.pkl"}


def test_every_python_module_it_runs_exists(text: str) -> None:
    modules = set(re.findall(r"python(?:\.exe)? -m ([\w.]+)", text))
    assert {"app.jobs.platform_admin", "tests.mutation", "scripts.doc_links", "alembic"} <= modules
    for module in sorted(modules):
        top = module.split(".")[0]
        if top == "app":
            base = REPOSITORY_ROOT / "backend" / Path(*module.split("."))
        elif top in {"scripts", "tests", "ml"}:
            base = REPOSITORY_ROOT / Path(*module.split("."))
        else:
            assert importlib.util.find_spec(module) is not None, module
            continue
        assert base.with_suffix(".py").exists() or (base / "__main__.py").exists(), module


def test_every_script_it_runs_exists(text: str) -> None:
    scripts = set(re.findall(r"python(?:\.exe)? (scripts/[\w]+\.py)", text))
    assert scripts == {"scripts/seed_demo.py", "scripts/testdb.py"}
    for script in scripts:
        assert (REPOSITORY_ROOT / script).is_file(), script


def test_every_screen_route_it_names_is_a_route_of_the_application(text: str) -> None:
    routes = set(re.findall(r":\s*'(/[a-z]*)'", ROUTES.read_text(encoding="utf-8")))
    named = set(re.findall(r"\(`(/[a-z]*)`\)", text))
    assert {"/", "/bookings", "/analytics", "/copilot", "/platform"} <= named
    assert named <= routes, named - routes
    # Every navigation area is walked through, not only some.
    assert routes - {"/login"} <= named, routes - named


def api_paths(openapi: dict[str, Any]) -> set[str]:
    return {re.sub(r"\{[^}]+\}", "{}", path) for path in openapi["paths"]}


def test_every_api_route_it_names_exists(text: str, openapi: dict[str, Any]) -> None:
    known = api_paths(openapi)
    named = {
        re.sub(r"\{[^}]+\}", "{}", match)
        for match in re.findall(r"(?:/api/v1|…)(/[\w/{}.-]*[\w}])", flat(text))
    }
    assert {"/analytics/overview", "/ml/demand-forecast", "/auth/login", "/documents"} <= named
    for path in named:
        assert any(api.endswith(path) for api in known), path


def test_the_api_counts_and_migration_head_it_states_are_the_codes(
    text: str, openapi: dict[str, Any]
) -> None:
    operations = sum(1 for item in openapi["paths"].values() for m in item if m in METHODS)
    assert f"{len(openapi['paths'])} paths and {operations} operations" in text
    chain = migration_chain()
    assert f"{len(chain)} linear revisions with head `{chain[-1]}`" in flat(text)
    assert f"`{chain[-1]} (head)`" in text
    assert f"-> {chain[-1]}`" in text


# --- the demo account and its data ----------------------------------------------------------


def test_the_demo_account_is_the_seeds_own_and_no_password_is_written_down(text: str) -> None:
    owner = seed_demo.DEFAULT_OWNER_EMAIL
    assert f"`{owner}`" in text
    assert f"grant {owner}" in text
    assert "export DEMO_OWNER_PASSWORD='<choose one>'" in text
    # The only values a reader is told to type are placeholders or generated.
    assert "<password>" in text and "<POSTGRES_PASSWORD>" in text
    assert "secrets.token_urlsafe(48)" in text
    for assignment in re.findall(r"(?:PASSWORD|SECRET_KEY)=(\S+)", text):
        assert assignment.startswith(("'<", '"$(')), assignment
    assert "The repository contains no demo password" in flat(text)


def test_the_seeded_facts_it_quotes_are_the_seeds(text: str) -> None:
    for hotel in seed_demo.HOTELS:
        assert f"*{hotel.name}*" in text, hotel.name
    harbour = seed_demo.HOTELS[0]
    assert harbour.name == "Demo Harbour Hotel"
    assert [room_type.code for room_type in harbour.room_types] == ["DBL", "SEA", "STE"]
    for room_type in harbour.room_types:
        assert f"`{room_type.code}` {room_type.name}" in text
    fixed = {code for code, _name, is_fixed in seed_demo.EXPENSE_CATEGORIES if is_fixed}
    assert "PAYROLL" in fixed and "`PAYROLL` carries a **Fixed cost** tag" in text
    assert f"seed `{seed_demo.DEFAULT_SEED}`" in text


def test_the_docker_seed_command_is_the_one_ci_runs(text: str) -> None:
    ci = CI.read_text(encoding="utf-8")
    fragment = '-e DEMO_OWNER_PASSWORD -v "$PWD/scripts:/app/scripts:ro" migrate'
    assert fragment in ci
    assert fragment in text
    assert "python scripts/seed_demo.py" in ci
    assert "app.jobs.platform_admin grant" in ci


def test_it_never_points_the_test_suite_at_the_demo_database(text: str) -> None:
    assert "**Never point `TEST_DATABASE_URL` at the demo database.**" in text
    # Every database the reader is told to seed, migrate or drop is a demo database.
    demo = set(re.findall(r"(?:5432/|DATABASE )(hotel_intelligence\w*)", text))
    assert demo == {"hotel_intelligence_demo", "hotel_intelligence"}
    assert not any(name.endswith("_test") for name in demo)
    assert "testdb.py create walkthrough_scratch_test" in text


# --- claims ---------------------------------------------------------------------------------


def test_it_keeps_the_synthetic_demo_data_apart_from_the_published_dataset(text: str) -> None:
    datasets = flat(section(text, "## Two datasets, never mixed"))
    assert "Synthetic demo data" in datasets and "Published evaluation dataset" in datasets
    source = "*Hotel booking demand datasets* (Antonio, de Almeida & Nunes, 2019), CC BY 4.0"
    assert source in datasets
    doi = re.search(r"10\.1016/j\.dib\.2018\.11\.126", TRAINING_DATA.read_text(encoding="utf-8"))
    assert doi is not None and doi.group(0) in datasets
    assert f"two real Portuguese hotels (2015{chr(0x2013)}2017)" in datasets
    assert "It is never used to train, evaluate or measure anything" in datasets
    whole = flat(text)
    assert "it is **not** the data any model was trained or evaluated on" in whole
    assert not re.search(r"(trained|evaluated|measured) on the (synthetic|demo) data", whole)


def test_it_states_the_portfolio_scope_and_never_claims_production_readiness(text: str) -> None:
    whole = flat(text)
    assert "academic and professional portfolio project (roadmap decision R5" in whole
    assert "**demo ready and not production ready**" in whole
    for claim in re.finditer(r"production[ -]ready", whole, flags=re.IGNORECASE):
        before = whole[max(0, claim.start() - 12) : claim.start()].lower()
        assert "not" in before, whole[claim.start() - 40 : claim.end()]
    limits = flat(section(text, "## Limitations to state during the demo"))
    for statement in (
        "**Portfolio scope; not production ready.**",
        "**Synthetic data.**",
        "**The served model is implemented, not validated.**",
        "are not established.**",
        "**Not implemented:** review sentiment",
    ):
        assert statement in limits, statement


def test_it_says_the_copilot_is_off_and_the_optional_path_unverified(text: str) -> None:
    copilot = flat(core_steps(text)["C13"])
    assert "**The copilot is switched off in this deployment**" in copilot
    assert "`503 LLM_DISABLED`" in copilot
    assert "**Optional, Route A only, not verified for this walkthrough:**" in copilot
    assert "The seed creates **no** knowledge documents" in copilot


def test_it_describes_the_clipping_as_fixed(text: str) -> None:
    whole = flat(text)
    assert "**Fixed 2026-11-27:** the expenses table used to clip its last column" in whole
    assert "**Fixed:** the Analytics expenses-table clipping, since 2026-11-27" in whole
    assert "Open, not fixed" not in whole
    assert not re.search(r"is (still )?clipped", whole)


# --- how a reader finds it ------------------------------------------------------------------


def test_the_readme_and_the_index_lead_to_the_walkthrough() -> None:
    readme = flat(README.read_text(encoding="utf-8"))
    # Beside the documentation index, and where the demo data is introduced.
    assert "follow the scripted [demo walkthrough](docs/demo-walkthrough.md)." in readme
    assert "The [demo walkthrough](docs/demo-walkthrough.md) puts these steps in order" in readme
    # One entry row in the index's tables, the kind `test_doc_links` counts, marked Current.
    assert index_entries()["docs/demo-walkthrough.md"] == 1
    rows = [
        line
        for line in INDEX.read_text(encoding="utf-8").splitlines()
        if line.startswith("| [demo-walkthrough.md](demo-walkthrough.md) | ")
    ]
    assert len(rows) == 1 and rows[0].endswith("| Current |"), rows
