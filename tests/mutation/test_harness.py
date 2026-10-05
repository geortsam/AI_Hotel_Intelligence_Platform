"""The mutation harness judges correctly, never edits the real tree, and its inventory is current.

These run in the normal suite and are fast: the harness is exercised against small throwaway
trees, with a fake executor except for one end-to-end check that runs real pytest. Running the
inventory itself is ``python -m tests.mutation`` (see ``harness.py``), not part of this file.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

import pytest

from tests.mutation import harness
from tests.mutation.harness import (
    Edit,
    HarnessError,
    Mutation,
    Outcome,
    Results,
    Runner,
    Verdict,
    apply_edits,
    child_environment,
    edits_by_path,
    exit_code,
    judge,
    mutated_bytes,
    parse_pytest_summary,
    parse_vitest_report,
    run,
    workspace,
)
from tests.mutation.inventory import MUTATIONS

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def mutation(
    id_: str = "T-M1",
    *,
    edits: tuple[Edit, ...] = (Edit("calc.py", "a + b", "a - b"),),
    killers: tuple[str, ...] = ("test_calc.py::test_add",),
    needs_database: bool = False,
) -> Mutation:
    return Mutation(id_, "T", "addition subtracts", edits, killers, needs_database=needs_database)


def silent(_: str) -> None:
    return None


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "calc.py").write_bytes(b"def add(a, b):\r\n    return a + b\r\n")
    (source / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n", encoding="utf-8"
    )
    return source


FILES = ["calc.py", "test_calc.py"]


# --- edits ---------------------------------------------------------------------------------------


def test_an_edit_applies_only_to_an_anchor_found_exactly_as_often_as_declared() -> None:
    assert apply_edits("x = 1\n", [Edit("f", "1", "2")]) == "x = 2\n"
    assert apply_edits("1 1", [Edit("f", "1", "2", occurrences=2)]) == "2 2"
    with pytest.raises(HarnessError, match="found 0 time"):
        apply_edits("x = 1\n", [Edit("f", "3", "2")])
    with pytest.raises(HarnessError, match="found 2 time"):
        apply_edits("1 1", [Edit("f", "1", "2")])


def test_edits_apply_in_order_and_later_anchors_see_earlier_edits() -> None:
    assert apply_edits("a", [Edit("f", "a", "b"), Edit("f", "b", "c")]) == "c"


def test_a_crlf_file_keeps_its_line_endings_and_lf_anchors_still_match() -> None:
    original = b"def add(a, b):\r\n    return a + b\r\n"
    edit = Edit("calc.py", "(a, b):\n    return a + b", "(a, b):\n    return a - b")

    assert mutated_bytes(original, [edit]) == b"def add(a, b):\r\n    return a - b\r\n"
    assert mutated_bytes(b"x\n", [Edit("f", "x", "y")]) == b"y\n"


def test_edits_are_grouped_by_file_in_their_declared_order() -> None:
    first, second, third = Edit("a", "1", "2"), Edit("b", "1", "2"), Edit("a", "2", "3")
    grouped = edits_by_path(mutation(edits=(first, second, third)))

    assert grouped == {"a": [first, third], "b": [second]}


# --- reading results -----------------------------------------------------------------------------


def test_the_pytest_summary_is_read_per_killer_and_per_parametrised_instance() -> None:
    output = "\n".join(
        [
            "PASSED t.py::test_a",
            "FAILED t.py::test_b - AssertionError: no",
            "PASSED t.py::test_c[one]",
            "FAILED t.py::test_c[two words] - boom",
            "PASSED t.py::test_d[x]",
            "PASSED t.py::test_d[y]",
            "PASSED t.py::test_e",
            "ERROR t.py::test_e - teardown",
        ]
    )
    killers = [
        "t.py::test_a",
        "t.py::test_b",
        "t.py::test_c",
        "t.py::test_d",
        "t.py::test_e",
        "t.py::test_c[one]",
        "t.py::test_missing",
        "t.py::test_d[z]",
    ]

    assert parse_pytest_summary(output, killers) == {
        "t.py::test_a": "passed",
        "t.py::test_b": "failed",
        "t.py::test_c": "failed",  # any instance failing kills a bare killer
        "t.py::test_d": "passed",
        "t.py::test_e": "failed",  # an error outranks the earlier pass
        "t.py::test_c[one]": "passed",  # a bracketed killer names exactly one instance
    }


def test_a_killer_prefix_does_not_match_a_longer_test_name() -> None:
    assert parse_pytest_summary("FAILED t.py::test_ab - x", ["t.py::test_a"]) == {}


def test_the_vitest_report_is_read_by_file_and_title() -> None:
    report: dict[str, object] = {
        "testResults": [
            {
                "name": "C:\\copy\\frontend\\src\\a.test.ts",
                "assertionResults": [
                    {"title": "contains no %s", "status": "passed"},
                    {"title": "uses no x", "status": "passed"},
                    {"title": "uses no x", "status": "failed"},
                    {"title": "skipped one", "status": "skipped"},
                ],
            },
            {
                "name": "/copy/frontend/src/b.test.ts",
                "assertionResults": [{"title": "uses no x", "status": "passed"}],
            },
        ]
    }
    killers = [
        "src/a.test.ts::uses no x",
        "src/b.test.ts::uses no x",
        "src/a.test.ts::skipped one",
        "src/a.test.ts::absent",
    ]

    assert parse_vitest_report(report, killers) == {
        "src/a.test.ts::uses no x": "failed",
        "src/b.test.ts::uses no x": "passed",
    }


# --- judging -------------------------------------------------------------------------------------


def test_a_mutation_is_killed_only_when_every_killer_fails() -> None:
    both = mutation(killers=("k1", "k2"))

    assert judge(both, {"k1": "failed", "k2": "failed"}).verdict is Verdict.KILLED
    survived = judge(both, {"k1": "failed", "k2": "passed"})
    assert survived.verdict is Verdict.SURVIVED
    assert "k2 (passed)" in survived.detail


def test_a_killer_that_never_ran_is_not_a_kill() -> None:
    """A mutation that breaks collection makes every test vanish, which proves nothing."""
    outcome = judge(mutation(killers=("k1",)), {})

    assert outcome.verdict is Verdict.SURVIVED
    assert "k1 (did not run)" in outcome.detail


def test_the_exit_code_fails_on_a_survivor_or_an_error_and_optionally_on_not_run() -> None:
    m = mutation()
    killed, survived = Outcome(m, Verdict.KILLED), Outcome(m, Verdict.SURVIVED)
    error, not_run = Outcome(m, Verdict.ERROR), Outcome(m, Verdict.NOT_RUN)

    assert exit_code([killed, not_run], require_all=False) == 0
    assert exit_code([killed, not_run], require_all=True) == 1
    assert exit_code([killed, survived], require_all=False) == 1
    assert exit_code([killed, error], require_all=False) == 1


def test_the_child_never_inherits_a_database_it_was_not_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://demo/seeded")

    assert "TEST_DATABASE_URL" not in child_environment(None)
    assert (
        child_environment("postgresql://x/y_test")["TEST_DATABASE_URL"] == "postgresql://x/y_test"
    )
    assert child_environment(None)["PYTHONDONTWRITEBYTECODE"] == "1"


# --- isolation -----------------------------------------------------------------------------------


class Recorder:
    """A fake executor: records what the copy looked like at each call, returns set results."""

    def __init__(
        self, killed: set[str] | None = None, *, raises: BaseException | None = None
    ) -> None:
        self.killed = killed or set()
        self.raises = raises
        self.seen: list[tuple[Path, bytes]] = []

    def __call__(self, copy: Path, _runner: Runner, killers: Sequence[str], _db: bool) -> Results:
        content = (copy / "calc.py").read_bytes()
        self.seen.append((copy, content))
        mutated = content != (b"def add(a, b):\r\n    return a + b\r\n")
        if mutated and self.raises is not None:
            raise self.raises
        return {k: ("failed" if mutated and k in self.killed else "passed") for k in killers}


def test_the_mutation_is_applied_in_a_copy_and_the_source_is_never_touched(tree: Path) -> None:
    before = (tree / "calc.py").read_bytes()
    recorder = Recorder(killed={"test_calc.py::test_add"})

    outcomes = run([mutation()], source=tree, files=FILES, execute=recorder, report=silent)

    assert [o.verdict for o in outcomes] == [Verdict.KILLED]
    (baseline_copy, baseline_content), (copy, mutated_content) = recorder.seen
    assert copy != tree and baseline_copy == copy
    assert baseline_content == before
    assert mutated_content == b"def add(a, b):\r\n    return a - b\r\n"
    assert (tree / "calc.py").read_bytes() == before
    assert not copy.exists(), "the copy is deleted after the run"


def test_each_mutation_starts_from_the_restored_original(tree: Path) -> None:
    recorder = Recorder(killed={"test_calc.py::test_add"})
    second = mutation("T-M2", edits=(Edit("calc.py", "return a + b", "return b + a"),))

    outcomes = run([mutation(), second], source=tree, files=FILES, execute=recorder, report=silent)

    assert [o.verdict for o in outcomes] == [Verdict.KILLED, Verdict.KILLED]
    assert recorder.seen[-1][1] == b"def add(a, b):\r\n    return b + a\r\n"


def test_a_survivor_is_reported_and_fails_the_run(tree: Path) -> None:
    outcomes = run([mutation()], source=tree, files=FILES, execute=Recorder(), report=silent)

    assert [o.verdict for o in outcomes] == [Verdict.SURVIVED]
    assert exit_code(outcomes, require_all=False) == 1


def test_a_failing_baseline_stops_the_run_before_any_mutation(tree: Path) -> None:
    def failing(_copy: Path, _runner: Runner, killers: Sequence[str], _db: bool) -> Results:
        return dict.fromkeys(killers, "failed")

    with pytest.raises(HarnessError, match="unmutated copy"):
        run([mutation()], source=tree, files=FILES, execute=failing, report=silent)


def test_a_stale_anchor_stops_the_run_before_anything_executes(tree: Path) -> None:
    recorder = Recorder()
    stale = mutation(edits=(Edit("calc.py", "a * b", "a / b"),))

    with pytest.raises(HarnessError, match="found 0 time"):
        run([stale], source=tree, files=FILES, execute=recorder, report=silent)
    assert recorder.seen == []


def test_an_execution_failure_is_an_error_and_the_copy_is_still_restored(tree: Path) -> None:
    recorder = Recorder(raises=subprocess.TimeoutExpired("pytest", 1))
    later = mutation("T-M2", edits=(Edit("calc.py", "def add", "def add "),))

    outcomes = run([mutation(), later], source=tree, files=FILES, execute=recorder, report=silent)

    assert [o.verdict for o in outcomes] == [Verdict.ERROR, Verdict.ERROR]
    assert "TimeoutExpired" in outcomes[0].detail
    # The second mutation saw the original with only its own edit: the first was reverted.
    assert recorder.seen[-1][1] == b"def add (a, b):\r\n    return a + b\r\n"


def test_an_interrupted_run_leaves_the_source_untouched_and_deletes_the_copy(tree: Path) -> None:
    before = (tree / "calc.py").read_bytes()
    recorder = Recorder(raises=KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        run([mutation()], source=tree, files=FILES, execute=recorder, report=silent)

    assert (tree / "calc.py").read_bytes() == before
    assert not recorder.seen[-1][0].exists()


def test_a_database_mutation_without_a_database_is_not_run(tree: Path) -> None:
    needs = mutation(needs_database=True)

    outcomes = run([needs], source=tree, files=FILES, execute=Recorder(), report=silent)

    assert [o.verdict for o in outcomes] == [Verdict.NOT_RUN]
    assert exit_code(outcomes, require_all=True) == 1


def test_deleting_the_copy_never_reaches_the_linked_node_modules(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "frontend" / "node_modules" / "pkg").mkdir(parents=True)
    sentinel = source / "frontend" / "node_modules" / "pkg" / "index.js"
    sentinel.write_text("real", encoding="utf-8")
    (source / "frontend" / "package.json").write_text("{}", encoding="utf-8")

    with workspace(source, ["frontend/package.json"]) as copy:
        linked = copy / "frontend" / "node_modules" / "pkg" / "index.js"
        assert linked.read_text(encoding="utf-8") == "real"

    assert not copy.exists()
    assert sentinel.read_text(encoding="utf-8") == "real"


def test_end_to_end_with_real_pytest_a_kill_a_survivor_and_a_broken_import(tree: Path) -> None:
    """The one check that runs real pytest in a subprocess, against a two-file tree."""
    killed = mutation("T-KILL")
    survives = mutation(
        "T-SURVIVE", edits=(Edit("calc.py", "def add(a, b):", "def add(a, b):  # noted"),)
    )
    # A syntax error removes the test from the run entirely: not a kill.
    breaks_import = mutation("T-IMPORT", edits=(Edit("calc.py", "return a + b", "return a +"),))

    outcomes = run([killed, survives, breaks_import], source=tree, files=FILES, report=silent)

    assert [o.verdict for o in outcomes] == [Verdict.KILLED, Verdict.SURVIVED, Verdict.SURVIVED]
    assert "did not run" in outcomes[2].detail


# --- the committed inventory ---------------------------------------------------------------------

RECORDED_PER_STAGE = {
    "7.6": 5,
    "7.7": 7,
    "7.8": 7,
    "7.9": 1,
    "7.10": 5,
    "7.11": 7,
    "7.12": 8,
    "7.13": 14,
    "7.14": 10,
    "F2": 3,
    "F16": 12,
    "I4": 14,
    "F1": 8,
    "F2-ADR": 6,
    "M1": 8,
    "M2": 8,
    "F2-COMP": 6,
}


def test_the_inventory_holds_every_recorded_mutation_once() -> None:
    assert Counter(m.stage for m in MUTATIONS) == RECORDED_PER_STAGE
    assert len({m.id for m in MUTATIONS}) == len(MUTATIONS)
    assert all(m.edits and m.killers for m in MUTATIONS)


@pytest.mark.parametrize("entry", MUTATIONS, ids=[m.id for m in MUTATIONS])
def test_every_edit_still_applies_to_the_source(entry: Mutation) -> None:
    """A refactor that moves an anchor fails here, in the normal suite, rather than at the
    next mutation run."""
    for relative, edits in edits_by_path(entry).items():
        mutated_bytes((REPOSITORY_ROOT / relative).read_bytes(), edits)


@pytest.mark.parametrize("entry", MUTATIONS, ids=[m.id for m in MUTATIONS])
def test_every_killer_names_a_test_that_exists(entry: Mutation) -> None:
    for killer in entry.killers:
        file, _, name = killer.partition("::")
        if entry.runner is Runner.VITEST:
            source = (REPOSITORY_ROOT / "frontend" / file).read_text(encoding="utf-8")
            title = re.escape(name)
            templated = re.sub(r"^(contains no|uses no) ", "", name)
            assert re.search(rf"\(\s*'{title}'", source) or (
                re.search(r"\)\(\s*'(contains no|uses no) %s'", source)
                and f"'{templated}'" in source
            ), killer
        else:
            function = name.split("[")[0]
            source = (REPOSITORY_ROOT / file).read_text(encoding="utf-8")
            assert re.search(rf"^def {re.escape(function)}\(", source, re.M), killer


def test_only_mutations_with_integration_killers_need_a_database() -> None:
    for entry in MUTATIONS:
        integration = any(k.startswith("tests/integration/") for k in entry.killers)
        assert entry.needs_database == integration, entry.id


def test_the_harness_imports_nothing_from_the_application() -> None:
    """It edits the application's files; importing them would load the unmutated originals."""
    source = Path(harness.__file__).read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from|import) (app|ml)\b", source, re.M)
