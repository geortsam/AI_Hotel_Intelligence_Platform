"""A2: the dependency vulnerability gate exists, covers both ecosystems and every lock, and bites.

Two halves. The workflow half reads .github/workflows/dependency-audit.yml, the scan's own
workflow since the A2 trigger correction, split on its own indentation as
test_ci_registry_mirror.py does (no YAML library is a declared test dependency), and holds
what the job runs, in what order, with what it is given -- and that a push to ANY branch
runs it. The behaviour half drives scripts/dependency_audit.py with a fake scanner -- no
network, no npm, no pip-audit -- and holds the decisions the wrapper makes: a finding fails, an
outage fails after its retries, an entry the scanner skipped fails, an unhashed lock is refused.

The live half -- that pip-audit 2.10.1 and npm really report the two fixtures' advisories -- is the
job's own "Prove the scanners report a known advisory" step, run before the real audits on every
workflow run; it needs the advisory services, so it cannot be a unit test.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection
from pathlib import Path
from typing import Any

import pytest

from scripts import dependency_audit as audit

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "dependency-audit.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
JOB_ID = "dependency-audit"
SCRIPT = "scripts/dependency_audit.py"
TOOL_LOCK = "scripts/dependency_audit_lock.txt"
#: A3's SBOM checker, installed in the image-reproducibility job from its own hashed lock.
SBOM_LOCK = "scripts/sbom_lock.txt"
BACKEND_LOCKS = ("backend/requirements-lock.txt", "backend/requirements-dev-lock.txt")
FIXTURES = ROOT / "tests" / "fixtures" / "dependency_audit"
PYTHON_CONTROL = ("tests/fixtures/dependency_audit/pyjwt-2.13.0.lock", "GHSA-ffc3-869f-jxw9")
NPM_CONTROL = ("tests/fixtures/dependency_audit", "GHSA-35jh-r3h4-6jhm")

JOB = re.compile(r"^  ([a-z][a-z0-9-]*):\n", re.MULTILINE)
STEP_START = re.compile(r"^      - name: (.+)$", re.MULTILINE)


def jobs() -> dict[str, str]:
    """job id -> its text, from `jobs:` to the next job."""
    text = WORKFLOW.read_text(encoding="utf-8")
    body = text[text.index("\njobs:\n") :]
    starts = list(JOB.finditer(body))
    return {
        match.group(1): body[match.end() : (starts[i + 1].start() if i + 1 < len(starts) else None)]
        for i, match in enumerate(starts)
    }


def audit_job() -> str:
    found = jobs()
    assert JOB_ID in found, "the dependency vulnerability scan job is missing from its workflow"
    return found[JOB_ID]


def steps() -> dict[str, str]:
    text = audit_job()
    starts = list(STEP_START.finditer(text))
    return {
        match.group(1).strip(): text[
            match.start() : (starts[i + 1].start() if i + 1 < len(starts) else None)
        ]
        for i, match in enumerate(starts)
    }


def run_lines() -> list[str]:
    """Every script line the job runs, comments dropped."""
    return [
        line.strip().removeprefix("run: ").strip("'")
        for text in steps().values()
        for line in text.splitlines()
        if "dependency_audit.py" in line and not line.strip().startswith("#")
    ]


def triggers(path: Path = WORKFLOW) -> dict[str, dict[str, list[str]]]:
    """The `on:` block: event -> filter -> values, read by indentation.

    Two spaces name an event, four a filter; a filter's values are a `[a, b]` flow list or
    `- item` lines below it. Anything else in the block fails the read rather than being skipped,
    so a construct this reader does not model cannot widen or narrow a trigger unseen.
    """
    text = path.read_text(encoding="utf-8")
    block = text[text.index("\non:\n") + len("\non:\n") : text.index("\nconcurrency:")]
    events: dict[str, dict[str, list[str]]] = {}
    event: str | None = None
    key: str | None = None
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if match := re.fullmatch(r"  ([a-z_]+):", line):
            event, key = match.group(1), None
            events[event] = {}
        elif event and (match := re.fullmatch(r"    ([a-z-]+):(?: \[(.*)\])?", line)):
            key = match.group(1)
            values = match.group(2)
            events[event][key] = (
                [v.strip().strip("'\"") for v in values.split(",")] if values else []
            )
        elif event and key and (match := re.fullmatch(r"      - (.+)", line)):
            events[event][key].append(match.group(1).strip().strip("'\""))
        else:
            raise AssertionError(f"an `on:` line this test does not model: {line!r}")
    return events


def glob_match(pattern: str, name: str) -> bool:
    """GitHub's filter globs: `*` matches anything but `/`, `**` anything at all."""
    regex = "".join(
        ".*" if part == "**" else "[^/]*" if part == "*" else re.escape(part)
        for part in re.split(r"(\*\*|\*)", pattern)
    )
    return re.fullmatch(regex, name) is not None


def runs_on(event: str, branch: str, changed: Collection[str] = ("README.md",)) -> bool:
    """Would a `push` of `branch` (or a pull request into it) start this workflow?

    GitHub's documented filter semantics, for the filters a push or pull request can carry:
    a branch must match `branches` if given and none of `branches-ignore`; a changed file must
    match `paths` if given, and the run is skipped when every changed file matches
    `paths-ignore`; an event that lists only `tags` ignores branch pushes.
    """
    filters = triggers().get(event)
    if filters is None:
        return False
    if "tags" in filters and "branches" not in filters and "branches-ignore" not in filters:
        return False
    if "branches" in filters and not any(glob_match(p, branch) for p in filters["branches"]):
        return False
    if any(glob_match(p, branch) for p in filters.get("branches-ignore", [])):
        return False
    if "paths" in filters and not any(glob_match(p, f) for p in filters["paths"] for f in changed):
        return False
    ignored = filters.get("paths-ignore", [])
    return not (ignored and all(any(glob_match(p, f) for p in ignored) for f in changed))


# --- the workflow -----------------------------------------------------------------------------


def test_the_dependency_vulnerability_gate_exists() -> None:
    assert "    name: Dependency vulnerability scan\n" in audit_job()
    assert list(steps()) == [
        "Check out the repository",
        "Set up Python",
        "Set up Node.js",
        "Install pip-audit from its hashed lock",
        "Prove the scanners report a known advisory",
        "Audit the shipped frontend dependencies",
        "Audit the Python locks",
    ]


def test_it_runs_on_every_push_and_pull_request_and_nothing_skips_it() -> None:
    assert triggers() == {"push": {}, "pull_request": {"branches": ["main"]}}
    job = audit_job()
    # No condition, and no dependency on another job that could skip it when that job fails.
    assert not re.search(r"^\s+if:", job, re.MULTILINE)
    assert "needs:" not in job
    assert "continue-on-error" not in job


@pytest.mark.parametrize(
    "branch", ["feature/sbom", "r2.10c3c-adjudication", "fix-123", "dependabot/npm/vite-9", "main"]
)
@pytest.mark.parametrize(
    "changed", [("README.md",), ("frontend/src/App.tsx",), ("backend/requirements-lock.txt",)]
)
def test_a_push_to_any_branch_triggers_the_scan(branch: str, changed: tuple[str, ...]) -> None:
    # The A2 trigger correction: the scan used to live in ci.yml, whose push trigger is
    # `branches: [main]`, so a feature branch without a pull request was never scanned.
    assert runs_on("push", branch, changed)


def test_the_push_trigger_carries_no_filter_at_all() -> None:
    assert triggers()["push"] == {}
    text = WORKFLOW.read_text(encoding="utf-8")
    on_block = text[text.index("\non:\n") : text.index("\nconcurrency:")]
    for word in ("branches-ignore", "paths", "paths-ignore", "tags", "types"):
        assert f"{word}:" not in on_block


def test_pull_requests_to_main_still_trigger_the_scan() -> None:
    assert runs_on("pull_request", "main")


def test_the_trigger_reader_sees_the_restriction_the_scan_used_to_have() -> None:
    # ci.yml keeps the old triggers; the reader must find them restrictive, or the tests above
    # could pass against a workflow that scans main only.
    assert triggers(CI_WORKFLOW)["push"] == {"branches": ["main"]}
    assert glob_match("main", "main")
    assert not glob_match("main", "feature/sbom")
    assert glob_match("feature/**", "feature/a/b")
    assert not glob_match("feature/*", "feature/a/b")


def test_ci_yml_no_longer_carries_the_scan_so_main_is_not_scanned_twice() -> None:
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "dependency-audit:" not in text
    assert "scripts/dependency_audit.py" not in text
    assert "name: Dependency vulnerability scan" not in text


def test_a_push_run_and_a_pull_request_run_never_cancel_each_other() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "group: dependency-audit-${{ github.event_name }}-${{ github.ref }}" in text


def test_the_job_is_given_no_secret_no_environment_and_no_token() -> None:
    job = audit_job()
    assert "secrets." not in job
    assert "github.token" not in job
    assert "GITHUB_TOKEN" not in job
    assert not re.search(r"^    env:", job, re.MULTILINE)
    assert "services:" not in job
    assert "persist-credentials: false" in steps()["Check out the repository"]
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "\npermissions:\n  contents: read\n" in text


def test_the_scanner_is_installed_from_its_hashed_lock_into_its_own_venv() -> None:
    install = steps()["Install pip-audit from its hashed lock"]
    assert 'python -m venv "$RUNNER_TEMP/pip-audit"' in install
    assert f"-m pip install --no-deps --require-hashes -r {TOOL_LOCK}" in install
    assert not re.search(r"^\s+cache:", steps()["Set up Python"], re.MULTILINE)
    # Every later step runs the venv's interpreter, never the runner's.
    assert run_lines()
    assert all(line.startswith('"$RUNNER_TEMP/pip-audit/bin/python" ') for line in run_lines())


def test_the_frontend_production_dependency_scan_is_present() -> None:
    assert f"{SCRIPT} npm frontend" in steps()["Audit the shipped frontend dependencies"]
    assert audit.NPM_AUDIT == ("audit", "--omit=dev", "--audit-level=high")


def test_both_backend_locks_and_the_scanners_own_are_scanned() -> None:
    (line,) = [line for line in run_lines() if f"{SCRIPT} python " in line]
    locks = line.split(f"{SCRIPT} python ", 1)[1].split()
    assert locks == [*BACKEND_LOCKS, TOOL_LOCK, SBOM_LOCK]


def test_the_positive_controls_run_before_the_real_audits() -> None:
    names = list(steps())
    prove = steps()["Prove the scanners report a known advisory"]
    assert f"{SCRIPT} expect-python-finding {' '.join(PYTHON_CONTROL)}" in prove
    assert f"{SCRIPT} expect-npm-finding {' '.join(NPM_CONTROL)}" in prove
    assert names.index("Prove the scanners report a known advisory") < names.index(
        "Audit the shipped frontend dependencies"
    )


# --- the pins and the fixtures -------------------------------------------------------------------


def test_the_pinned_scanner_version_is_the_one_its_lock_installs() -> None:
    lock = audit.parse_lock((ROOT / TOOL_LOCK).read_text(encoding="utf-8"))
    by_name = {entry.name: entry for entry in lock}
    assert by_name["pip-audit"].version == audit.PIP_AUDIT_VERSION == "2.10.1"
    assert all(entry.hashes for entry in lock)
    declared = (ROOT / "scripts" / "dependency_audit_requirements.txt").read_text(encoding="utf-8")
    assert f"\npip-audit=={audit.PIP_AUDIT_VERSION}\n" in declared


@pytest.mark.parametrize("lock", [*BACKEND_LOCKS, TOOL_LOCK, SBOM_LOCK])
def test_every_entry_of_a_real_lock_is_parsed_and_hashed(lock: str) -> None:
    text = (ROOT / lock).read_text(encoding="utf-8")
    pins = re.findall(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*==", text, re.MULTILINE)
    entries = audit.parse_lock(text)
    assert len(entries) == len(pins) > 0
    assert all(entry.hashes >= 1 for entry in entries)


def test_the_python_control_pins_the_vulnerable_pyjwt() -> None:
    (entry,) = audit.parse_lock((ROOT / PYTHON_CONTROL[0]).read_text(encoding="utf-8"))
    assert (entry.name, entry.version, entry.hashes) == ("pyjwt", "2.13.0", 2)


def test_the_npm_control_ships_the_vulnerable_lodash() -> None:
    manifest = json.loads((FIXTURES / "npm-package.json").read_text(encoding="utf-8"))
    lock = json.loads((FIXTURES / "npm-package-lock.json").read_text(encoding="utf-8"))
    assert manifest["dependencies"] == {"lodash": "4.17.20"}
    assert "devDependencies" not in manifest
    entry = lock["packages"]["node_modules/lodash"]
    assert entry["version"] == "4.17.20"
    assert not entry.get("dev")
    assert set(audit.NPM_FIXTURE_FILES.values()) == {"package.json", "package-lock.json"}


def test_the_real_locks_no_longer_pin_the_vulnerable_versions() -> None:
    versions = {
        entry.name: entry.version
        for lock in BACKEND_LOCKS
        for entry in audit.parse_lock((ROOT / lock).read_text(encoding="utf-8"))
    }
    assert versions["pyjwt"] == "2.15.1"
    assert versions["certifi"] == "2026.10.23"


# --- the wrapper's decisions ----------------------------------------------------------------------

LOCK_TEXT = """\
# a comment
alpha==1.0 \\
    --hash=sha256:aaaa
    # via -r requirements.txt
beta==2.0 ; sys_platform == 'no-such-platform' \\
    --hash=sha256:bbbb \\
    --hash=sha256:cccc
    # via alpha
"""


def pip_report(*deps: tuple[str, str, list[str]]) -> dict[str, Any]:
    return {
        "dependencies": [
            {
                "name": name,
                "version": version,
                "vulns": [{"id": i, "fix_versions": []} for i in ids],
            }
            for name, version, ids in deps
        ]
    }


def write_lock(tmp_path: Path, text: str = LOCK_TEXT) -> Path:
    path = tmp_path / "lock.txt"
    path.write_text(text, encoding="utf-8")
    return path


class FakePipAudit:
    """Answers each requirements file with the report for the names it contains."""

    def __init__(self, vulns: dict[str, list[str]] | None = None, skip: Collection[str] = ()):
        self.vulns = vulns or {}
        self.skip = skip
        self.files: list[str] = []

    def __call__(self, requirements: Path) -> audit.Attempt:
        text = requirements.read_text(encoding="utf-8")
        self.files.append(text)
        entries = [e for e in audit.parse_lock(text) if e.name not in self.skip]
        here = [e for e in entries if not e.marker or audit.marker_holds(e)]
        report = pip_report(*((e.name, e.version, self.vulns.get(e.name, [])) for e in here))
        failed = any(self.vulns.get(e.name) for e in here)
        return audit.Attempt(1 if failed else 0, report)


def test_a_clean_lock_passes_and_every_entry_is_audited(tmp_path: Path) -> None:
    fake = FakePipAudit()
    findings, count = audit.audit_python_lock(write_lock(tmp_path), fake)
    assert (findings, count) == ([], 2)


def test_a_known_advisory_fails_the_python_gate(tmp_path: Path) -> None:
    fake = FakePipAudit(vulns={"alpha": ["GHSA-test-high"]})
    findings, _ = audit.audit_python_lock(write_lock(tmp_path), fake)
    assert findings == ["alpha 1.0: GHSA-test-high -- fixed in no released version"]


def test_entries_for_other_platforms_are_audited_in_a_second_pass(tmp_path: Path) -> None:
    fake = FakePipAudit(vulns={"beta": ["GHSA-windows-only"]})
    findings, count = audit.audit_python_lock(write_lock(tmp_path), fake)
    assert count == 2
    assert findings == ["beta 2.0: GHSA-windows-only -- fixed in no released version"]
    # The second file is beta alone, its marker removed and its hashes kept.
    assert fake.files[1].splitlines()[:3] == [
        "beta==2.0 \\",
        "    --hash=sha256:bbbb \\",
        "    --hash=sha256:cccc",
    ]


def test_an_entry_the_scanner_did_not_audit_fails_the_gate(tmp_path: Path) -> None:
    with pytest.raises(audit.AuditIncompleteError, match="alpha"):
        audit.audit_python_lock(write_lock(tmp_path), FakePipAudit(skip={"alpha"}))


def test_hash_enforcement_is_active(tmp_path: Path) -> None:
    assert "--require-hashes" in audit.PIP_AUDIT_FLAGS
    assert "--strict" in audit.PIP_AUDIT_FLAGS
    fake = FakePipAudit()
    unhashed = write_lock(tmp_path, "alpha==1.0 \\\n    --hash=sha256:aaaa\ngamma==3.0\n")
    with pytest.raises(audit.AuditIncompleteError, match="without a hash: \\['gamma'\\]"):
        audit.audit_python_lock(unhashed, fake)
    assert fake.files == []


def test_an_advisory_service_outage_fails_after_every_retry() -> None:
    calls: list[int] = []
    waits: list[float] = []

    def outage() -> audit.Attempt:
        calls.append(1)
        return audit.Attempt(1, None, "connection refused")

    with pytest.raises(audit.AuditUnavailableError, match="no report after 3 attempts"):
        audit.run_with_retries(outage, what="x", sleep=waits.append, log=lambda _: None)
    assert len(calls) == audit.ATTEMPTS == 3
    assert waits == list(audit.DELAYS)


def test_a_transient_failure_is_retried_and_the_report_then_counts() -> None:
    answers = [audit.Attempt(1, None, "timeout"), audit.Attempt(0, {"dependencies": []})]
    result = audit.run_with_retries(
        lambda: answers.pop(0), what="x", sleep=lambda _: None, log=lambda _: None
    )
    assert result.report == {"dependencies": []}


def test_an_outage_exits_two_and_a_finding_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = write_lock(tmp_path)
    monkeypatch.setattr(audit, "check_scanner_version", lambda: None)
    monkeypatch.setattr(audit.time, "sleep", lambda _: None)
    monkeypatch.setattr(audit, "pip_audit_attempt", lambda _: audit.Attempt(1, None, "down"))
    assert audit.main(["python", str(lock)]) == 2
    monkeypatch.setattr(audit, "pip_audit_attempt", FakePipAudit(vulns={"alpha": ["GHSA-x"]}))
    assert audit.main(["python", str(lock)]) == 1
    monkeypatch.setattr(audit, "pip_audit_attempt", FakePipAudit())
    assert audit.main(["python", str(lock)]) == 0


def npm_report(severities: dict[str, str]) -> dict[str, Any]:
    vulnerabilities = {
        name: {"severity": severity, "via": [{"url": f"https://github.com/advisories/{name}"}]}
        for name, severity in severities.items()
    }
    counts = {
        s: list(severities.values()).count(s) for s in ("low", "moderate", "high", "critical")
    }
    return {
        "auditReportVersion": 2,
        "vulnerabilities": vulnerabilities,
        "metadata": {"vulnerabilities": counts},
    }


@pytest.mark.parametrize("severity", ["high", "critical"])
def test_high_severity_findings_fail_the_gate(severity: str) -> None:
    assert severity in audit.FAILING_NPM_SEVERITIES
    report = npm_report({"lodash": severity})
    findings, _ = audit.audit_npm(Path("."), lambda _: audit.Attempt(1, report))
    assert findings == [f"lodash ({severity}): https://github.com/advisories/lodash"]


def test_a_moderate_finding_is_below_the_frontend_threshold() -> None:
    report = npm_report({"left-pad": "moderate"})
    findings, counts = audit.audit_npm(Path("."), lambda _: audit.Attempt(0, report))
    assert findings == []
    assert counts["moderate"] == 1


def test_npm_exit_code_and_report_must_agree() -> None:
    report = npm_report({"lodash": "high"})
    with pytest.raises(audit.AuditIncompleteError, match="exited 0"):
        audit.audit_npm(Path("."), lambda _: audit.Attempt(0, report))


def test_an_npm_error_object_is_an_outage_not_a_report(monkeypatch: pytest.MonkeyPatch) -> None:
    error = {"message": "request to the advisory endpoint failed", "error": {"summary": ""}}

    class Completed:
        returncode = 1
        stdout = json.dumps(error)

    monkeypatch.setattr(audit.shutil, "which", lambda _: "npm")
    monkeypatch.setattr(audit.subprocess, "run", lambda *a, **k: Completed())
    attempt = audit.npm_attempt(Path("."))
    assert attempt.report is None
    assert attempt.detail == "request to the advisory endpoint failed"


def test_a_control_that_finds_nothing_fails() -> None:
    assert audit.expect([], "GHSA-ffc3-869f-jxw9", "control") == 1
    assert audit.expect(["pyjwt 2.13.0: GHSA-ffc3-869f-jxw9"], "GHSA-ffc3-869f-jxw9", "c") == 0
