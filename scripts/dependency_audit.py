"""A2: the dependency vulnerability gate CI runs on every workflow run.

Two scanners, one policy -- a known advisory in a dependency fails the run:

* **frontend**, the shipped dependency tree: ``npm audit --omit=dev --audit-level=high`` against
  frontend/package-lock.json. A high or critical advisory fails it.
* **backend**, every hashed Python lock: ``pip-audit --require-hashes --disable-pip --strict``.
  pip-audit has no severity option (PyPI's vulnerability data carries no severity), so ANY
  known advisory fails it -- a superset of "high and critical".

What this wrapper adds to the two commands, and why each is needed:

* **An outage never passes.** Both tools exit 1 for "advisories found" and for "could not reach
  the advisory service", so the exit code alone cannot tell them apart. An attempt counts only
  if it produced a real report (npm: a JSON object with ``auditReportVersion``; pip-audit: the
  ``-o`` JSON file, which it writes only after it has queried every dependency). An attempt
  without one is retried ``ATTEMPTS`` times; after that the gate fails with exit 2, by name.
* **Every lock entry is audited.** pip-audit evaluates each requirement's environment marker
  against the interpreter running it and silently leaves out the entries whose marker is false
  -- on the Linux runner, a universal lock's Windows-only ``tzdata`` and ``colorama``. The
  report's audited set must equal the lock's entries whose marker holds, exactly; the others
  are audited in a second pass from a copy with their markers removed (hashes kept). A lock
  entry audited by neither pass fails the gate.
* **A fresh advisory cache.** pip-audit caches PyPI's JSON responses in pip's HTTP cache by
  default; each run here gets an empty temporary cache, so a stale answer cannot be replayed.
* **A positive control.** ``expect-python-finding`` / ``expect-npm-finding`` run the same code
  against fixtures that pin a known-vulnerable version and fail unless the named advisory is
  reported. CI runs them first, so a scanner that silently reports nothing fails the job.

Exit codes: 0 clean, 1 advisories found (or a control that found nothing), 2 the audit could not
be completed. Run with the interpreter that has pip-audit installed (CI: its own venv, from
scripts/dependency_audit_lock.txt); this module itself needs only the standard library and
``packaging``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

from packaging.markers import Marker
from packaging.utils import canonicalize_name

#: The pinned scanner. scripts/dependency_audit_lock.txt pins the same version, with hashes.
PIP_AUDIT_VERSION = "2.10.1"
#: The flags every pip-audit run uses. ``--require-hashes`` refuses a lock entry without a hash;
#: ``--disable-pip`` audits exactly the pinned entries instead of re-resolving them; ``--strict``
#: fails the run when any dependency cannot be collected rather than skipping it.
PIP_AUDIT_FLAGS = ("--require-hashes", "--disable-pip", "--strict", "--progress-spinner", "off")
#: The frontend scan: the shipped tree only, failing at high and above.
NPM_AUDIT = ("audit", "--omit=dev", "--audit-level=high")
#: npm severities at or above the threshold. Used to cross-check npm's own exit code.
FAILING_NPM_SEVERITIES = ("high", "critical")
ATTEMPTS = 3
#: Seconds to wait before the second and the third attempt.
DELAYS = (10.0, 30.0)
#: The names under which a fixture directory holds its npm manifest and lockfile. Not the
#: real file names, so no dependency scanner mistakes the fixture for a project.
NPM_FIXTURE_FILES = {
    "npm-package.json": "package.json",
    "npm-package-lock.json": "package-lock.json",
}


class AuditUnavailableError(Exception):
    """No attempt produced a report: the advisory service or the tool failed every time."""


class AuditIncompleteError(Exception):
    """A report was produced but does not cover what was asked of it."""


@dataclass(frozen=True)
class Attempt:
    returncode: int
    #: The parsed report, or None when the attempt produced no usable one.
    report: dict[str, Any] | None
    detail: str = ""


def run_with_retries(
    attempt: Callable[[], Attempt],
    *,
    what: str,
    attempts: int = ATTEMPTS,
    delays: Sequence[float] = DELAYS,
    sleep: Callable[[float], None] | None = None,
    log: Callable[[str], None] = print,
) -> Attempt:
    """The first attempt that produced a report; AuditUnavailableError if none did."""
    sleep = sleep or time.sleep
    for number in range(1, attempts + 1):
        result = attempt()
        if result.report is not None:
            return result
        log(f"  attempt {number}/{attempts} of {what} produced no report: {result.detail}")
        if number < attempts:
            sleep(delays[number - 1])
    raise AuditUnavailableError(f"{what}: no report after {attempts} attempts")


# --- Python locks ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class LockEntry:
    name: str
    version: str
    marker: str
    #: The entry's lines as the lock has them: the requirement line, its hashes, its comments.
    lines: tuple[str, ...]

    @property
    def hashes(self) -> int:
        return sum("--hash=sha256:" in line for line in self.lines)

    def without_marker(self) -> str:
        head = f"{self.name}=={self.version} \\"
        return "\n".join((head, *self.lines[1:]))


def parse_lock(text: str) -> list[LockEntry]:
    """Every ``name==version`` entry of a uv-generated hashed lock, with its marker."""
    entries: list[LockEntry] = []
    current: list[str] = []

    def close() -> None:
        if not current:
            return
        requirement = current[0].rstrip("\\").strip()
        spec, _, marker = requirement.partition(";")
        name, separator, version = spec.strip().partition("==")
        if not separator or not version:
            raise AuditIncompleteError(f"not an exact pin: {current[0]!r}")
        entries.append(
            LockEntry(canonicalize_name(name), version.strip(), marker.strip(), tuple(current))
        )
        current.clear()

    for line in text.splitlines():
        if line and not line[0].isspace() and not line.startswith("#"):
            close()
            current.append(line)
        elif current and line.strip():
            current.append(line)
    close()
    return entries


def marker_holds(entry: LockEntry) -> bool:
    return not entry.marker or Marker(entry.marker).evaluate()


def pip_audit_attempt(requirements: Path) -> Attempt:
    with tempfile.TemporaryDirectory() as scratch:
        output = Path(scratch) / "report.json"
        command = [
            sys.executable,
            "-m",
            "pip_audit",
            "-r",
            str(requirements),
            *PIP_AUDIT_FLAGS,
            "--format",
            "json",
            "--output",
            str(output),
            "--cache-dir",
            str(Path(scratch) / "cache"),
            "--timeout",
            "30",
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if not output.is_file():
            tail = (completed.stderr or completed.stdout).strip().splitlines()[-1:]
            return Attempt(completed.returncode, None, tail[0] if tail else "no output")
        try:
            report = json.loads(output.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return Attempt(completed.returncode, None, f"unreadable report: {exc}")
        if not isinstance(report, dict) or "dependencies" not in report:
            return Attempt(completed.returncode, None, "the report has no dependency list")
        return Attempt(completed.returncode, report)


def audited_names(report: dict[str, Any]) -> set[str]:
    """The dependencies pip-audit actually queried (a skipped one carries ``skip_reason``)."""
    return {
        canonicalize_name(dep["name"]) for dep in report["dependencies"] if "skip_reason" not in dep
    }


def python_findings(report: dict[str, Any]) -> list[str]:
    return [
        f"{dep['name']} {dep['version']}: {vuln['id']}"
        + (f" ({', '.join(vuln['aliases'])})" if vuln.get("aliases") else "")
        + f" -- fixed in {', '.join(vuln.get('fix_versions') or []) or 'no released version'}"
        for dep in report["dependencies"]
        for vuln in dep.get("vulns", [])
    ]


def audit_python_lock(
    lock: Path, attempt: Callable[[Path], Attempt] | None = None
) -> tuple[list[str], int]:
    """(findings, number of entries audited) for one lock; raises if any entry went unaudited."""
    scan = attempt or pip_audit_attempt
    entries = parse_lock(lock.read_text(encoding="utf-8"))
    if not entries:
        raise AuditIncompleteError(f"{lock}: no pinned entries")
    # pip-audit's --require-hashes refuses the same lock; checked here first so the refusal names
    # every entry at once and is not mistaken for an outage and retried.
    unhashed = [e.name for e in entries if not e.hashes]
    if unhashed:
        raise AuditIncompleteError(f"{lock}: entries without a hash: {unhashed}")
    here = {e.name for e in entries if marker_holds(e)}
    elsewhere = [e for e in entries if not marker_holds(e)]

    result = run_with_retries(lambda: scan(lock), what=f"pip-audit {lock}")
    assert result.report is not None
    audited = audited_names(result.report)
    if audited != here:
        raise AuditIncompleteError(
            f"{lock}: pip-audit audited {sorted(audited ^ here)} differently from the lock's "
            "entries for this platform"
        )
    findings = python_findings(result.report)
    if result.returncode != 0 and not findings:
        raise AuditIncompleteError(f"{lock}: pip-audit exited {result.returncode} with no finding")

    if elsewhere:
        with tempfile.TemporaryDirectory() as scratch:
            copy = Path(scratch) / f"{lock.stem}-other-platforms.txt"
            copy.write_text("\n".join(e.without_marker() for e in elsewhere) + "\n", "utf-8")
            other = run_with_retries(lambda: scan(copy), what=f"pip-audit {lock} (markers)")
        assert other.report is not None
        if audited_names(other.report) != {e.name for e in elsewhere}:
            raise AuditIncompleteError(f"{lock}: the other-platform entries were not all audited")
        findings += python_findings(other.report)
        if other.returncode != 0 and not python_findings(other.report):
            raise AuditIncompleteError(
                f"{lock}: pip-audit exited {other.returncode} with no finding"
            )
    return findings, len(audited) + len(elsewhere)


# --- npm ------------------------------------------------------------------------------------------


def npm_attempt(directory: Path) -> Attempt:
    npm = shutil.which("npm")
    if npm is None:
        raise AuditIncompleteError("npm is not on PATH")
    completed = subprocess.run(
        [npm, *NPM_AUDIT, "--json"],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return Attempt(completed.returncode, None, "npm printed no JSON report")
    if not isinstance(report, dict) or "auditReportVersion" not in report:
        message = report.get("message", "no report") if isinstance(report, dict) else "no report"
        return Attempt(completed.returncode, None, str(message))
    return Attempt(completed.returncode, report)


def npm_findings(report: dict[str, Any]) -> list[str]:
    """Each package with a high or critical advisory, with the advisories' URLs."""
    found = []
    for name, vulnerability in sorted(report.get("vulnerabilities", {}).items()):
        if vulnerability.get("severity") not in FAILING_NPM_SEVERITIES:
            continue
        urls = sorted({via["url"] for via in vulnerability.get("via", []) if isinstance(via, dict)})
        found.append(
            f"{name} ({vulnerability['severity']}): {', '.join(urls) or 'via a dependency'}"
        )
    return found


def audit_npm(
    directory: Path, attempt: Callable[[Path], Attempt] | None = None
) -> tuple[list[str], dict[str, int]]:
    scan = attempt or npm_attempt
    result = run_with_retries(lambda: scan(directory), what=f"npm audit in {directory}")
    assert result.report is not None
    counts = result.report.get("metadata", {}).get("vulnerabilities", {})
    findings = npm_findings(result.report)
    # npm's exit code is the threshold's verdict; the counts are cross-checked against it, so a
    # report and an exit code that disagree fail rather than pass.
    if bool(findings) != (result.returncode != 0):
        raise AuditIncompleteError(
            f"npm audit exited {result.returncode} but reported {len(findings)} high or "
            "critical finding(s)"
        )
    return findings, counts


# --- the commands ---------------------------------------------------------------------------------


def check_scanner_version() -> None:
    installed = metadata.version("pip-audit")
    if installed != PIP_AUDIT_VERSION:
        raise AuditIncompleteError(
            f"pip-audit {installed} is installed; {PIP_AUDIT_VERSION} is pinned"
        )


def command_python(locks: Sequence[str]) -> int:
    check_scanner_version()
    failed = False
    for lock in map(Path, locks):
        findings, count = audit_python_lock(lock)
        print(f"{lock}: {count} pinned entries audited, {len(findings)} known advisories")
        for finding in findings:
            print(f"  FOUND {finding}")
        failed = failed or bool(findings)
    return 1 if failed else 0


def command_npm(directory: str) -> int:
    findings, counts = audit_npm(Path(directory))
    print(f"npm {' '.join(NPM_AUDIT)} in {directory}: {json.dumps(counts, sort_keys=True)}")
    for finding in findings:
        print(f"  FOUND {finding}")
    return 1 if findings else 0


def expect(found: list[str], advisory: str, what: str) -> int:
    if any(advisory in finding for finding in found):
        print(f"OK   {what}: the scanner reports {advisory}, as it must")
        return 0
    print(f"FAIL {what}: the scanner did not report {advisory}; it reported {found}")
    return 1


def command_expect_python(lock: str, advisory: str) -> int:
    check_scanner_version()
    findings, _ = audit_python_lock(Path(lock))
    return expect(findings, advisory, f"pip-audit {lock}")


def command_expect_npm(fixture: str, advisory: str) -> int:
    with tempfile.TemporaryDirectory() as scratch:
        for source, target in NPM_FIXTURE_FILES.items():
            shutil.copyfile(Path(fixture) / source, Path(scratch) / target)
        findings, _ = audit_npm(Path(scratch))
    return expect(findings, advisory, f"npm audit {fixture}")


USAGE = """usage:
  dependency_audit.py python LOCK [LOCK ...]
  dependency_audit.py npm DIRECTORY
  dependency_audit.py expect-python-finding LOCK ADVISORY_ID
  dependency_audit.py expect-npm-finding FIXTURE_DIRECTORY ADVISORY_ID"""


def main(argv: Sequence[str]) -> int:
    match list(argv):
        case ["python", *locks] if locks:
            run = lambda: command_python(locks)  # noqa: E731
        case ["npm", directory]:
            run = lambda: command_npm(directory)  # noqa: E731
        case ["expect-python-finding", lock, advisory]:
            run = lambda: command_expect_python(lock, advisory)  # noqa: E731
        case ["expect-npm-finding", fixture, advisory]:
            run = lambda: command_expect_npm(fixture, advisory)  # noqa: E731
        case _:
            print(USAGE, file=sys.stderr)
            return 2
    try:
        return run()
    except (AuditUnavailableError, AuditIncompleteError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
