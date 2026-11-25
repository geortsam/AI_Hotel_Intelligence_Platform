"""The decision register says which decisions are taken, and every summary agrees with it.

The register is §3 of ``docs/roadmap-v2.0-execution.md``: thirteen briefs, §3.1-§3.13. On
2026-11-25 the project owner decided one of them, R5 (§3.13): Academic / Professional Portfolio.
These tests hold that record in place:

* §3.13 carries the decision, its status, its authority and its scope, and keeps its ID;
* no other brief is marked decided -- R5 decides none of them, and R0.1 in particular still needs
  a separate authorization to merge PR #1 or release;
* the rows the decision defers keep their place in the matrix, marked, not deleted;
* the completion and closure audits count the open decisions as the register does.
"""

from __future__ import annotations

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPOSITORY_ROOT / "docs"
REGISTER = DOCS / "roadmap-v2.0-execution.md"

DECIDED_STATUS = "- **Status:** Decided."
#: The rows R5's portfolio target defers. Each must still be in the matrix, and marked.
DEFERRED_ROWS = (
    "R1.2",
    "R1.4a",
    "R2.1",
    "R2.2",
    "R2.3",
    "R2.4",
    "R3.1",
    "R3.2",
    "R3.4",
    "R4.1",
    "R4.3a",
    "R4.3b",
)
DEFERRED_MARK = "**deferred, outside the current R5 target**"


def flat(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\n[ \t]*>", "\n", text))


def briefs() -> dict[str, str]:
    """Each brief of §3, by its number ("3.1" ... "3.13"), and its text."""
    text = REGISTER.read_text(encoding="utf-8")
    section = text[text.index("## 3. Decision briefs") : text.index("## 4. Stage log")]
    parts = re.split(r"^### (3\.\d+) ", section, flags=re.MULTILINE)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def decided() -> list[str]:
    return sorted((n for n, body in briefs().items() if DECIDED_STATUS in body), key=str)


def test_the_register_keeps_thirteen_briefs() -> None:
    assert list(briefs()) == [f"3.{n}" for n in range(1, 14)]


def test_r5_is_recorded_as_decided_by_the_project_owner() -> None:
    body = flat(briefs()["3.13"])
    assert briefs()["3.13"].startswith("Product reach (R5)")
    for field in (
        "- **Decision:** Academic / Professional Portfolio.",
        DECIDED_STATUS,
        "- **Decision authority:** Project owner.",
        "- **Scope:** University presentation, professional portfolio, GitHub showcase and "
        "technical demonstration.",
        "- **Conditions:** none.",
    ):
        assert flat(field) in body, field


def test_r5_decides_no_other_brief() -> None:
    assert decided() == ["3.13"]
    # R0.1 in particular: merging PR #1 or releasing still needs its own authorization.
    assert "Decided" not in briefs()["3.1"]


def test_the_rows_the_decision_defers_stay_in_the_matrix_marked() -> None:
    text = REGISTER.read_text(encoding="utf-8")
    for row in DEFERRED_ROWS:
        lines = [line for line in text.splitlines() if line.startswith(f"| **{row}** ")]
        assert len(lines) == 1, row
        assert DEFERRED_MARK in lines[0], row
    r5 = [line for line in text.splitlines() if line.startswith("| **R5** ")]
    assert len(r5) == 1 and "decided 2026-11-25" in r5[0]


def test_the_audits_count_the_open_decisions_as_the_register_does() -> None:
    open_count = len(briefs()) - len(decided())
    assert open_count == 12
    completion = flat((DOCS / "project-completion-audit.md").read_text(encoding="utf-8"))
    assert f"## 5. Remaining user decisions — {open_count} open briefs, R5 decided" in completion
    assert f"**Remaining user decisions:** {open_count} open decision briefs" in completion
    closure = flat((DOCS / "project-closure-audit.md").read_text(encoding="utf-8"))
    assert f"| Open user decisions | **{open_count}** " in closure
    assert f"**Open decisions: {open_count}**, not 13." in closure
