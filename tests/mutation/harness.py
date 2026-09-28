"""Re-run the stage reports' mutation checks, reproducibly, without touching the working tree.

Stages 7.6-7.14, F2 and F16 each broke their own guarantees on purpose and reported that a test
caught every break. Those runs were one-off scripts: the evidence was the report, and nothing
could re-run it. This module re-runs them from the committed inventory in ``inventory.py``.

## How one mutation is judged

A mutation is a set of exact text edits and the named tests that must fail when they are applied
(its *killers*). It is **killed** only when every killer runs and fails. It **survives** when any
killer passes, is skipped, or never runs -- a mutation that stops a module importing at all
makes its tests error at collection, which proves nothing about the guarantee, so it is not
accepted as a kill.

Before any mutation runs, every killer is run once against the unmutated copy and must pass.
Otherwise a test that already fails would "catch" every mutation pointed at it.

## Why the working tree is never edited

The tracked files are copied to a temporary directory and every edit is made there. An
interrupted run -- a crash, Ctrl-C, a killed CI job -- leaves at worst a stale directory under
the system temp folder, never a modified source file. Inside the copy, each mutation is still
reverted in a ``finally`` and the reverted bytes are checked against the originals, so one
mutation cannot leak into the next. The real files each mutation names are hashed before and
after the run, and the run fails if any changed.

``frontend/node_modules`` is linked into the copy rather than copied (a junction on Windows, a
symlink elsewhere); the link is removed before the copy is deleted, so deleting the copy never
reaches the real dependencies.

## Databases

Mutations whose killers need PostgreSQL run only when ``--database-url`` names a disposable
database (``scripts/testdb.py create``): the integration suite truncates and migrates it, and
``tests/db_safety.py`` refuses anything else. The harness never reads ``TEST_DATABASE_URL`` from
the environment, because on a developer machine that may point at seeded demo data. Without a
URL those mutations are reported as not run -- which ``--require-all`` turns into a failure.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

#: Generous: the slowest killer (the Stage 7.14 refit) takes about a minute on a laptop.
SUBPROCESS_TIMEOUT_SECONDS = 1200


class Runner(StrEnum):
    PYTEST = "pytest"
    VITEST = "vitest"


@dataclass(frozen=True)
class Edit:
    """Replace ``old`` with ``new`` in ``path``; ``old`` must occur ``occurrences`` times."""

    path: str
    old: str
    new: str
    occurrences: int = 1


@dataclass(frozen=True)
class Mutation:
    id: str
    stage: str
    breaks: str
    edits: tuple[Edit, ...]
    #: pytest node ids, or ``<path under frontend/>::<test title>`` for Vitest.
    killers: tuple[str, ...]
    runner: Runner = Runner.PYTEST
    needs_database: bool = False
    note: str = ""


class Verdict(StrEnum):
    KILLED = "killed"
    SURVIVED = "SURVIVED"
    NOT_RUN = "not run"
    ERROR = "ERROR"


@dataclass(frozen=True)
class Outcome:
    mutation: Mutation
    verdict: Verdict
    detail: str = ""


class HarnessError(RuntimeError):
    """The harness could not judge a mutation (a bad anchor, a failing baseline, a timeout)."""


# --- edits ---------------------------------------------------------------------------------------


def apply_edits(text: str, edits: Sequence[Edit]) -> str:
    """Apply edits in order to LF-normalised text; each anchor must occur as declared."""
    for edit in edits:
        found = text.count(edit.old)
        if found != edit.occurrences:
            raise HarnessError(
                f"{edit.path}: anchor found {found} time(s), expected {edit.occurrences}: "
                f"{edit.old[:60]!r}"
            )
        text = text.replace(edit.old, edit.new)
    return text


def mutated_bytes(original: bytes, edits: Sequence[Edit]) -> bytes:
    """The file with ``edits`` applied, keeping its line endings (CRLF on a Windows checkout)."""
    text = original.decode("utf-8")
    crlf = "\r\n" in text
    result = apply_edits(text.replace("\r\n", "\n"), edits)
    return (result.replace("\n", "\r\n") if crlf else result).encode("utf-8")


def edits_by_path(mutation: Mutation) -> dict[str, list[Edit]]:
    grouped: dict[str, list[Edit]] = {}
    for edit in mutation.edits:
        grouped.setdefault(edit.path, []).append(edit)
    return grouped


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- the isolated copy ---------------------------------------------------------------------------


def tracked_files(root: Path) -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
    ).stdout.decode("utf-8")
    return [name for name in listed.split("\0") if name and (root / name).is_file()]


def link_directory(target: Path, link: Path) -> None:
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def unlink_directory(link: Path) -> None:
    """Remove a link made by :func:`link_directory` without touching what it points at."""
    if link.is_symlink() or (sys.platform == "win32" and link.is_junction()):
        if sys.platform == "win32":
            os.rmdir(link)
        else:
            link.unlink()


@contextmanager
def workspace(
    source: Path, files: Sequence[str], *, link_node_modules: bool = True
) -> Iterator[Path]:
    """A throwaway copy of ``files`` from ``source``, deleted on exit whatever happens."""
    copy = Path(tempfile.mkdtemp(prefix="ahip-mutation-"))
    node_modules = copy / "frontend" / "node_modules"
    try:
        for name in files:
            destination = copy / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / name, destination)
        real_modules = source / "frontend" / "node_modules"
        if link_node_modules and real_modules.is_dir() and node_modules.parent.is_dir():
            link_directory(real_modules, node_modules)
        yield copy
    finally:
        unlink_directory(node_modules)
        shutil.rmtree(copy, ignore_errors=True)


@contextmanager
def applied(copy: Path, mutation: Mutation) -> Iterator[None]:
    """Apply ``mutation`` inside ``copy``; restore every file byte-for-byte on exit."""
    originals: dict[Path, bytes] = {}
    try:
        for relative, edits in edits_by_path(mutation).items():
            path = copy / relative
            original = path.read_bytes()
            changed = mutated_bytes(original, edits)
            originals[path] = original
            path.write_bytes(changed)
        yield
    finally:
        for path, original in originals.items():
            path.write_bytes(original)
            if path.read_bytes() != original:
                raise HarnessError(f"{path}: could not be restored")


# --- running killers -----------------------------------------------------------------------------

#: What one run reports per killer: "passed", "failed", or absent when it did not run.
Results = dict[str, str]
Execute = Callable[[Path, Runner, Sequence[str], bool], Results]


def child_environment(database_url: str | None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != "TEST_DATABASE_URL"}
    # A mutation of the same size written within the same second as the original could be
    # served from a stale .pyc; writing none rules that out.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if database_url is not None:
        env["TEST_DATABASE_URL"] = database_url
    return env


SUMMARY_WORDS = (("PASSED", "passed"), ("FAILED", "failed"), ("ERROR", "failed"))


def summary_verdicts(output: str) -> dict[str, str]:
    """Every node id in ``-rA`` summary lines (``PASSED <id>``, ``FAILED <id> - <why>``)."""
    verdicts: dict[str, str] = {}
    for line in output.splitlines():
        for word, verdict in SUMMARY_WORDS:
            if line.startswith(f"{word} "):
                node = line[len(word) + 1 :].split(" - ", 1)[0]
                # A failure outranks a pass: a test that errors in teardown is reported twice.
                if verdicts.get(node) != "failed":
                    verdicts[node] = verdict
    return verdicts


def parse_pytest_summary(output: str, killers: Sequence[str]) -> Results:
    """A killer names one test, or -- without ``[...]`` -- every parametrised instance of it:
    it failed if any instance failed, and passed only if every instance ran and passed."""
    verdicts = summary_verdicts(output)
    results: Results = {}
    for killer in killers:
        instances = [
            verdict
            for node, verdict in verdicts.items()
            if node == killer or ("[" not in killer and node.startswith(killer + "["))
        ]
        if "failed" in instances:
            results[killer] = "failed"
        elif instances:
            results[killer] = "passed"
    return results


def vitest_verdicts(report: dict[str, object]) -> list[tuple[str, str, str]]:
    """(file, title, status) for every test in a Vitest JSON report."""
    suites = report.get("testResults", [])
    assert isinstance(suites, list)
    return [
        (
            str(suite.get("name", "")).replace("\\", "/"),
            str(test.get("title")),
            str(test.get("status")),
        )
        for suite in suites
        for test in suite.get("assertionResults", [])
    ]


def parse_vitest_report(report: dict[str, object], killers: Sequence[str]) -> Results:
    """A title shared by several tests in one file (``it.each``) is judged like a parametrised
    pytest killer: failed if any failed, passed only if all passed."""
    verdicts = vitest_verdicts(report)
    results: Results = {}
    for killer in killers:
        file, _, title = killer.partition("::")
        statuses = [s for name, t, s in verdicts if name.endswith("/" + file) and t == title]
        if "failed" in statuses:
            results[killer] = "failed"
        elif statuses and all(status == "passed" for status in statuses):
            results[killer] = "passed"
    return results


def make_executor(database_url: str | None) -> Execute:
    def execute(copy: Path, runner: Runner, killers: Sequence[str], database: bool) -> Results:
        env = child_environment(database_url if database else None)
        if runner is Runner.PYTEST:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    *killers,
                    "-p",
                    "no:cacheprovider",
                    "--no-header",
                    "-rA",
                    "--tb=no",
                ],
                cwd=copy,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=SUBPROCESS_TIMEOUT_SECONDS,
            )
            return parse_pytest_summary(completed.stdout, killers)

        node = shutil.which("node")
        frontend = copy / "frontend"
        if node is None or not (frontend / "node_modules" / "vitest" / "vitest.mjs").exists():
            raise HarnessError("Vitest mutations need node and an installed frontend/node_modules")
        files = sorted({killer.partition("::")[0] for killer in killers})
        report_path = copy / "vitest-report.json"
        report_path.unlink(missing_ok=True)
        subprocess.run(
            [
                node,
                "node_modules/vitest/vitest.mjs",
                "run",
                *files,
                "--reporter=json",
                f"--outputFile={report_path}",
            ],
            cwd=frontend,
            env=env,
            capture_output=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
        if not report_path.exists():
            return {}
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert isinstance(report, dict)
        return parse_vitest_report(report, killers)

    return execute


# --- the run -------------------------------------------------------------------------------------


def judge(mutation: Mutation, results: Results) -> Outcome:
    failed = [killer for killer in mutation.killers if results.get(killer) == "failed"]
    if len(failed) == len(mutation.killers):
        return Outcome(mutation, Verdict.KILLED, f"{len(failed)} killer(s) failed")
    alive = [
        f"{killer} ({results.get(killer, 'did not run')})"
        for killer in mutation.killers
        if results.get(killer) != "failed"
    ]
    return Outcome(mutation, Verdict.SURVIVED, "; ".join(alive))


def runnable(mutation: Mutation, *, database_url: str | None, frontend: bool) -> str | None:
    """Why this mutation cannot run here, or None."""
    if mutation.needs_database and database_url is None:
        return "needs --database-url"
    if mutation.runner is Runner.VITEST and not frontend:
        return "frontend mutations disabled"
    return None


def run(
    mutations: Sequence[Mutation],
    *,
    source: Path = REPOSITORY_ROOT,
    files: Sequence[str] | None = None,
    execute: Execute | None = None,
    database_url: str | None = None,
    frontend: bool = True,
    report: Callable[[str], None] = print,
) -> list[Outcome]:
    execute = execute or make_executor(database_url)
    touched = sorted({source / edit.path for m in mutations for edit in m.edits})
    before = {path: digest(path) for path in touched}

    outcomes: list[Outcome] = []
    selected: list[Mutation] = []
    for mutation in mutations:
        reason = runnable(mutation, database_url=database_url, frontend=frontend)
        if reason is None:
            selected.append(mutation)
        else:
            outcomes.append(Outcome(mutation, Verdict.NOT_RUN, reason))

    with workspace(source, files if files is not None else tracked_files(source)) as copy:
        # Every edit must apply before anything runs: a stale anchor is an inventory error.
        for mutation in selected:
            for relative, edits in edits_by_path(mutation).items():
                mutated_bytes((copy / relative).read_bytes(), edits)

        baseline_failures = baseline(copy, selected, execute)
        if baseline_failures:
            raise HarnessError(
                "killers fail on the unmutated copy: " + ", ".join(baseline_failures)
            )
        report(f"baseline: every killer passes on the unmutated copy ({len(selected)} mutations)")

        for mutation in selected:
            try:
                with applied(copy, mutation):
                    results = execute(
                        copy, mutation.runner, mutation.killers, mutation.needs_database
                    )
                outcome = judge(mutation, results)
            except (HarnessError, subprocess.TimeoutExpired, OSError) as failure:
                outcome = Outcome(mutation, Verdict.ERROR, f"{type(failure).__name__}: {failure}")
            outcomes.append(outcome)
            report(f"{outcome.verdict:<9} {mutation.id:<10} {mutation.breaks} -- {outcome.detail}")

    changed = [str(path) for path in touched if digest(path) != before[path]]
    if changed:
        raise HarnessError(f"the working tree changed during the run: {changed}")
    order = {m.id: index for index, m in enumerate(mutations)}
    return sorted(outcomes, key=lambda outcome: order[outcome.mutation.id])


def baseline(copy: Path, mutations: Sequence[Mutation], execute: Execute) -> list[str]:
    failures: list[str] = []
    groups: dict[tuple[Runner, bool], list[str]] = {}
    for mutation in mutations:
        groups.setdefault((mutation.runner, mutation.needs_database), []).extend(mutation.killers)
    for (runner, database), killers in groups.items():
        unique = list(dict.fromkeys(killers))
        results = execute(copy, runner, unique, database)
        failures.extend(k for k in unique if results.get(k) != "passed")
    return failures


def exit_code(outcomes: Sequence[Outcome], *, require_all: bool) -> int:
    bad = {Verdict.SURVIVED, Verdict.ERROR} | ({Verdict.NOT_RUN} if require_all else set())
    return 1 if any(outcome.verdict in bad for outcome in outcomes) else 0


# --- maintenance ---------------------------------------------------------------------------------


def discover(
    mutations: Sequence[Mutation],
    *,
    source: Path = REPOSITORY_ROOT,
    database_url: str | None = None,
    report: Callable[[str], None] = print,
) -> None:
    """For each mutation, run every test in its killers' files and print what fails.

    For choosing killers when adding a mutation or after a refactor; it judges nothing."""
    with workspace(source, tracked_files(source)) as copy:
        for mutation in mutations:
            files = sorted({killer.partition("::")[0] for killer in mutation.killers})
            env = child_environment(database_url if mutation.needs_database else None)
            with applied(copy, mutation):
                if mutation.runner is Runner.PYTEST:
                    completed = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "pytest",
                            *files,
                            "-p",
                            "no:cacheprovider",
                            "--no-header",
                            "-rA",
                            "--tb=no",
                        ],
                        cwd=copy,
                        env=env,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=SUBPROCESS_TIMEOUT_SECONDS,
                    )
                    failed = [
                        n for n, v in summary_verdicts(completed.stdout).items() if v == "failed"
                    ]
                else:
                    node = shutil.which("node") or "node"
                    report_path = copy / "vitest-report.json"
                    report_path.unlink(missing_ok=True)
                    subprocess.run(
                        [
                            node,
                            "node_modules/vitest/vitest.mjs",
                            "run",
                            *files,
                            "--reporter=json",
                            f"--outputFile={report_path}",
                        ],
                        cwd=copy / "frontend",
                        env=env,
                        capture_output=True,
                        timeout=SUBPROCESS_TIMEOUT_SECONDS,
                    )
                    loaded = (
                        json.loads(report_path.read_text(encoding="utf-8"))
                        if report_path.exists()
                        else {}
                    )
                    failed = [
                        f"{name}::{title}"
                        for name, title, status in vitest_verdicts(loaded)
                        if status == "failed"
                    ]
            report(f"{mutation.id}: {len(failed)} failing")
            for name in failed:
                report(f"    {name}")
